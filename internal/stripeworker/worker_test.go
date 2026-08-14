package stripeworker

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync/atomic"
	"testing"
)

func TestWorkerZeroAmountApproveAndRedirect(t *testing.T) {
	var confirmCalls atomic.Int32
	var approved atomic.Bool
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		body, _ := io.ReadAll(request.Body)
		writer.Header().Set("Content-Type", "application/json")
		var payload map[string]any
		switch request.URL.Path {
		case "/v1/payment_pages/cs_test_worker/init":
			payload = zeroPayload("paypal", "card")
		case "/v1/elements/sessions":
			payload = map[string]any{"session_id": "elements_test", "config_id": "config_test", "total_summary": map[string]any{"due": 0}, "currency": "eur"}
		case "/v1/payment_methods":
			payload = map[string]any{"id": "pm_test_paypal"}
		case "/backend-api/payments/checkout/snapshot":
			payload = map[string]any{"result": "updated"}
		case "/v1/payment_pages/cs_test_worker":
			payload = zeroPayload("paypal", "card")
			if request.Method == http.MethodGet && approved.Load() {
				payload["next_action"] = map[string]any{"redirect_to_url": map[string]any{"url": "https://pm-redirects.stripe.com/authorize/test"}}
			}
		case "/v1/payment_pages/cs_test_worker/confirm":
			confirmCalls.Add(1)
			form, _ := url.ParseQuery(string(body))
			if form.Get("payment_method") != "pm_test_paypal" || form.Has("payment_method_data[type]") {
				t.Errorf("unexpected confirm form: %v", form)
			}
			payload = map[string]any{"submission_attempt": map[string]any{"state": "requires_approval"}}
		case "/v1/payment_pages/cs_test_worker/poll":
			payload = map[string]any{"state": "pending"}
		case "/backend-api/payments/checkout/approve":
			approved.Store(true)
			payload = map[string]any{"result": "approved"}
		default:
			t.Fatalf("unexpected route %s", request.URL.Path)
		}
		if err := json.NewEncoder(writer).Encode(payload); err != nil {
			t.Errorf("encode response: %v", err)
		}
	}))
	defer server.Close()

	worker := testWorker(server.URL)
	redirect, err := worker.run(context.Background())
	if err != nil {
		t.Fatalf("worker.run: %v", err)
	}
	if redirect != "https://pm-redirects.stripe.com/authorize/test" {
		t.Fatalf("redirect = %q", redirect)
	}
	if confirmCalls.Load() != 1 {
		t.Fatalf("confirm calls = %d", confirmCalls.Load())
	}
}

func TestWorkerPromoUpdateFailureStopsBeforeConfirm(t *testing.T) {
	var confirmCalls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		payload := map[string]any{"total_summary": map[string]any{"due": 100}, "currency": "eur", "payment_method_types": []any{"paypal"}}
		if request.URL.Path == "/backend-api/payments/checkout/update" {
			writer.WriteHeader(http.StatusConflict)
			payload = map[string]any{"error": map[string]any{"message": "promotion rejected"}}
		}
		if strings.HasSuffix(request.URL.Path, "/confirm") {
			confirmCalls.Add(1)
		}
		_ = json.NewEncoder(writer).Encode(payload)
	}))
	defer server.Close()

	worker := testWorker(server.URL)
	_, err := worker.run(context.Background())
	if err == nil || !strings.Contains(err.Error(), "后置优惠更新失败") {
		t.Fatalf("error = %v", err)
	}
	if confirmCalls.Load() != 0 {
		t.Fatalf("confirm should not be sent, got %d", confirmCalls.Load())
	}
}

func TestWorkerDetectsPayPalThenAppliesPromoAndConfirmsWithPaymentMethod(t *testing.T) {
	var initCalls atomic.Int32
	var promoCalls atomic.Int32
	var confirmCalls atomic.Int32
	var sequence []string
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		body, _ := io.ReadAll(request.Body)
		writer.Header().Set("Content-Type", "application/json")
		payload := zeroPayload("paypal", "card")
		switch request.URL.Path {
		case "/v1/payment_pages/cs_test_worker/init":
			sequence = append(sequence, "init")
			if initCalls.Add(1) == 1 {
				payload["total_summary"] = map[string]any{"due": 1933, "total": 1933}
			}
		case "/v1/elements/sessions":
			sequence = append(sequence, "elements")
			payload["session_id"] = "elements_test"
			payload["config_id"] = "elements_config_test"
		case "/backend-api/payments/checkout/update":
			sequence = append(sequence, "promo")
			promoCalls.Add(1)
			if request.Header.Get("OpenAI-Sentinel-Token") != "" {
				t.Error("promo update must not include Sentinel headers")
			}
			var update map[string]any
			if err := json.Unmarshal(body, &update); err != nil {
				t.Fatal(err)
			}
			campaign, _ := update["promo_campaign"].(map[string]any)
			if campaign["promo_campaign_id"] != "plus-1-month-free" {
				t.Fatalf("promo campaign = %v", campaign)
			}
		case "/v1/payment_pages/cs_test_worker":
			sequence = append(sequence, "tax")
		case "/v1/payment_methods":
			sequence = append(sequence, "payment_method")
			form, _ := url.ParseQuery(string(body))
			if form.Get("type") != "paypal" || form.Get("billing_details[address][country]") != "DE" {
				t.Fatalf("payment method form = %v", form)
			}
			payload = map[string]any{"id": "pm_test_paypal"}
		case "/backend-api/payments/checkout/snapshot":
			sequence = append(sequence, "snapshot")
			if request.Header.Get("OpenAI-Sentinel-Token") != "" {
				t.Error("snapshot must not include Sentinel headers")
			}
			var snapshot map[string]any
			if err := json.Unmarshal(body, &snapshot); err != nil {
				t.Fatal(err)
			}
			billing := snapshot["snapshot"].(map[string]any)["billing_address"].(map[string]any)
			address := billing["address"].(map[string]any)
			if address["country"] != "DE" {
				t.Fatalf("snapshot address = %v", address)
			}
			payload = map[string]any{"result": "updated"}
		case "/v1/payment_pages/cs_test_worker/confirm":
			sequence = append(sequence, "confirm")
			confirmCalls.Add(1)
			form, _ := url.ParseQuery(string(body))
			if form.Get("payment_method") != "pm_test_paypal" || form.Has("payment_method_data[type]") {
				t.Fatalf("confirm form = %v", form)
			}
			payload = map[string]any{"next_action": map[string]any{"redirect_to_url": map[string]any{"url": "https://pm-redirects.stripe.com/authorize/promo-test"}}}
		default:
			t.Fatalf("unexpected route %s", request.URL.Path)
		}
		_ = json.NewEncoder(writer).Encode(payload)
	}))
	defer server.Close()

	worker := testWorker(server.URL)
	redirect, err := worker.run(context.Background())
	if err != nil {
		t.Fatalf("worker.run: %v", err)
	}
	if redirect != "https://pm-redirects.stripe.com/authorize/promo-test" {
		t.Fatalf("redirect = %q", redirect)
	}
	if promoCalls.Load() != 1 || confirmCalls.Load() != 1 {
		t.Fatalf("promo=%d confirm=%d", promoCalls.Load(), confirmCalls.Load())
	}
	joined := strings.Join(sequence, ",")
	if !strings.Contains(joined, "init,elements,promo,init,elements,tax,snapshot,payment_method,confirm") {
		t.Fatalf("sequence = %s", joined)
	}
}

func TestWorkerStopsPollingOnPostApproveGenericDecline(t *testing.T) {
	var approved atomic.Bool
	var fullPollCalls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		payload := zeroPayload("paypal")
		switch request.URL.Path {
		case "/v1/payment_pages/cs_test_worker/init", "/v1/elements/sessions":
		case "/backend-api/payments/checkout/snapshot":
			payload = map[string]any{"result": "updated"}
		case "/v1/payment_methods":
			payload = map[string]any{"id": "pm_test_paypal"}
		case "/v1/payment_pages/cs_test_worker/confirm":
			payload = map[string]any{"submission_attempt": map[string]any{"state": "requires_approval"}}
		case "/v1/payment_pages/cs_test_worker/poll":
			payload = map[string]any{"payment_object_status": "requires_payment_method"}
		case "/backend-api/payments/checkout/approve":
			approved.Store(true)
			payload = map[string]any{"result": "approved"}
		case "/v1/payment_pages/cs_test_worker":
			if request.Method == http.MethodGet {
				fullPollCalls.Add(1)
				if approved.Load() {
					payload = map[string]any{
						"submission_attempt": map[string]any{"state": "failed", "error": map[string]any{"payment_error": map[string]any{"code": "setup_attempt_failed", "decline_code": "generic_decline"}}},
						"setup_intent":       map[string]any{"status": "requires_payment_method", "last_setup_error": map[string]any{"code": "setup_attempt_failed", "decline_code": "generic_decline"}},
					}
				}
			}
		default:
			t.Fatalf("unexpected route %s", request.URL.Path)
		}
		_ = json.NewEncoder(writer).Encode(payload)
	}))
	defer server.Close()

	worker := testWorker(server.URL)
	_, err := worker.run(context.Background())
	if err == nil || !strings.Contains(err.Error(), "generic_decline") {
		t.Fatalf("error = %v", err)
	}
	if fullPollCalls.Load() != 2 {
		t.Fatalf("full poll calls = %d; expected one before and one after approve", fullPollCalls.Load())
	}
}

func TestWorkerNonZeroAfterTaxStopsBeforeConfirm(t *testing.T) {
	var confirmCalls atomic.Int32
	server := newWorkerTestServer(t, func(path string) map[string]any {
		if strings.HasSuffix(path, "/confirm") {
			confirmCalls.Add(1)
		}
		if path == "/v1/payment_pages/cs_test_worker" {
			return map[string]any{"total_summary": map[string]any{"due": 250}, "currency": "eur", "payment_method_types": []any{"paypal"}}
		}
		return zeroPayload("paypal")
	})
	defer server.Close()

	worker := testWorker(server.URL)
	_, err := worker.run(context.Background())
	if err == nil || !strings.Contains(err.Error(), "后置优惠未同步") {
		t.Fatalf("error = %v", err)
	}
	if confirmCalls.Load() != 0 {
		t.Fatalf("confirm should not be sent, got %d", confirmCalls.Load())
	}
}

func TestWorkerExplicitMethodsWithoutPayPalStopBeforeConfirm(t *testing.T) {
	var confirmCalls atomic.Int32
	var promoCalls atomic.Int32
	server := newWorkerTestServer(t, func(path string) map[string]any {
		if path == "/backend-api/payments/checkout/update" {
			promoCalls.Add(1)
		}
		if strings.HasSuffix(path, "/confirm") {
			confirmCalls.Add(1)
		}
		return zeroPayload("card")
	})
	defer server.Close()

	worker := testWorker(server.URL)
	_, err := worker.run(context.Background())
	if err == nil || !strings.Contains(err.Error(), "Stripe init 未开放 PayPal") {
		t.Fatalf("error = %v", err)
	}
	if confirmCalls.Load() != 0 {
		t.Fatalf("confirm should not be sent, got %d", confirmCalls.Load())
	}
	if promoCalls.Load() != 0 {
		t.Fatalf("promo update should not be sent, got %d", promoCalls.Load())
	}
}

func TestWorkerPayPalExpressTokenDoesNotOverrideCardOnlyInit(t *testing.T) {
	var confirmCalls atomic.Int32
	server := newWorkerTestServer(t, func(path string) map[string]any {
		payload := zeroPayload("card", "link")
		if path == "/v1/elements/sessions" {
			payload["session_id"] = "elements_test"
			payload["config_id"] = "config_test"
			payload["paypal_express_config"] = map[string]any{"client_token": nil}
		}
		if strings.HasSuffix(path, "/confirm") {
			confirmCalls.Add(1)
		}
		return payload
	})
	defer server.Close()

	worker := testWorker(server.URL)
	_, err := worker.run(context.Background())
	if err == nil || !strings.Contains(err.Error(), "Stripe init 未开放 PayPal") {
		t.Fatalf("error = %v", err)
	}
	if confirmCalls.Load() != 0 {
		t.Fatalf("confirm calls = %d", confirmCalls.Load())
	}
}

func TestWorkerMissingInitMethodsDefaultsToCardAndStops(t *testing.T) {
	var confirmCalls atomic.Int32
	server := newWorkerTestServer(t, func(path string) map[string]any {
		payload := zeroPayload()
		delete(payload, "payment_method_types")
		if strings.HasSuffix(path, "/confirm") {
			confirmCalls.Add(1)
		}
		return payload
	})
	defer server.Close()

	worker := testWorker(server.URL)
	_, err := worker.run(context.Background())
	if err == nil || !strings.Contains(err.Error(), "methods=card") {
		t.Fatalf("error = %v", err)
	}
	if confirmCalls.Load() != 0 {
		t.Fatalf("confirm calls = %d", confirmCalls.Load())
	}
}

func TestWorkerDiagnosticsDoNotContainInputSecrets(t *testing.T) {
	server := newWorkerTestServer(t, func(path string) map[string]any {
		payload := zeroPayload("paypal")
		payload["debug"] = "access.secret@example.com Cookie=cookie-secret sentinel-secret socks5://user:pass@proxy:1080"
		if path == "/v1/payment_methods" {
			payload["id"] = "pm_test_diagnostic"
		}
		if strings.HasSuffix(path, "/confirm") {
			payload["next_action"] = map[string]any{"redirect_to_url": map[string]any{"url": "https://pm-redirects.stripe.com/authorize/test"}}
		}
		return payload
	})
	defer server.Close()

	worker := testWorker(server.URL)
	if _, err := worker.run(context.Background()); err != nil {
		t.Fatalf("worker.run: %v", err)
	}
	raw, err := json.Marshal(worker.diagnostics)
	if err != nil {
		t.Fatal(err)
	}
	text := string(raw)
	for _, secret := range []string{"access.secret@example.com", "cookie-secret", "sentinel-secret", "socks5://user:pass@proxy:1080", "Billing Person"} {
		if strings.Contains(text, secret) {
			t.Fatalf("diagnostics leaked %q: %s", secret, text)
		}
	}
}

func TestLocaleForBillingCountry(t *testing.T) {
	tests := map[string][2]string{
		"DE": {"de-DE", "Europe/Berlin"},
		"BR": {"pt-BR", "America/Sao_Paulo"},
		"XX": {"en-US", "UTC"},
	}
	for country, expected := range tests {
		locale, timezone := localeForCountry(country)
		if locale != expected[0] || timezone != expected[1] {
			t.Fatalf("%s = %s/%s", country, locale, timezone)
		}
	}
}

func TestWorkerUsesProvidedLocaleProfile(t *testing.T) {
	worker := &worker{input: Input{
		Country:         "US",
		BrowserLocale:   "es-MX",
		BrowserTimezone: "America/Mexico_City",
	}}
	locale, timezone := worker.localeProfile()
	if locale != "es-MX" || timezone != "America/Mexico_City" {
		t.Fatalf("profile = %s/%s", locale, timezone)
	}
}

func TestWorkerUsesFirefoxUserAgent(t *testing.T) {
	if !strings.Contains(stripeUserAgent, "Firefox/147.0") {
		t.Fatalf("user agent = %s", stripeUserAgent)
	}
}

func TestEmptyAddressFieldsAreOmittedFromStripeForms(t *testing.T) {
	worker := testWorker("https://stripe.invalid")
	checkout := &checkoutContext{initChecksum: "checksum", amount: "0", uiMode: "hosted"}
	form := worker.confirmForm(zeroPayload("paypal"), checkout, "pm_test_paypal")

	if form.Has("payment_method_data[type]") {
		t.Fatal("confirm must not create an inline payment method")
	}
	if got := form.Get("payment_method"); got != "pm_test_paypal" {
		t.Fatalf("payment method = %q", got)
	}

	taxForm := url.Values{}
	setNonEmpty(taxForm, "tax_region[state]", worker.input.Billing.Address.State)
	setNonEmpty(taxForm, "tax_region[country]", worker.input.Billing.Address.Country)
	if taxForm.Has("tax_region[state]") {
		t.Fatal("empty tax state must not be sent")
	}
	if got := taxForm.Get("tax_region[country]"); got != "DE" {
		t.Fatalf("tax country = %q", got)
	}
}

func newWorkerTestServer(t *testing.T, response func(path string) map[string]any) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		_, _ = io.ReadAll(request.Body)
		writer.Header().Set("Content-Type", "application/json")
		if err := json.NewEncoder(writer).Encode(response(request.URL.Path)); err != nil {
			t.Errorf("encode response: %v", err)
		}
	}))
}

func testWorker(base string) *worker {
	input := Input{
		SessionID:       "cs_test_worker",
		PublishableKey:  "pk_test_worker",
		ProxyURL:        "socks5://user:pass@proxy:1080",
		AccessToken:     "access.secret@example.com",
		CookieHeader:    "cookie-secret",
		DeviceID:        "device-test",
		Country:         "DE",
		BrowserLocale:   "de-DE",
		BrowserTimezone: "Europe/Berlin",
		ProcessorEntity: "openai_ie",
		CheckoutURL:     "https://chatgpt.com/checkout/openai_ie/cs_test_worker",
		Billing: Billing{
			Name:    "Billing Person",
			Email:   "billing.secret@example.com",
			Address: Address{Country: "DE", Line1: "Test 1", City: "Berlin", PostalCode: "10115"},
		},
		ApproveHeaders: map[string]string{"OpenAI-Sentinel-Token": "sentinel-secret"},
		ApplyPromo:     true,
		StripeBase:     base,
		ChatGPTBase:    base,
	}
	client, err := newTLSClient("")
	if err != nil {
		panic(err)
	}
	return &worker{input: input, stripe: client, chatGPT: client}
}

func zeroPayload(methods ...string) map[string]any {
	items := make([]any, len(methods))
	for index, method := range methods {
		items[index] = method
	}
	return map[string]any{
		"total_summary":        map[string]any{"due": 0, "total": 0},
		"currency":             "eur",
		"payment_method_types": items,
		"init_checksum":        "checksum_test",
		"config_id":            "config_test",
		"ui_mode":              "hosted",
	}
}
