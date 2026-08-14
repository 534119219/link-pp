package stripeworker

import (
	"encoding/json"
	"strings"
	"testing"
)

func decodeFixture(t *testing.T, raw string) map[string]any {
	t.Helper()
	decoder := json.NewDecoder(strings.NewReader(raw))
	decoder.UseNumber()
	var payload map[string]any
	if err := decoder.Decode(&payload); err != nil {
		t.Fatal(err)
	}
	return payload
}

func TestCheckoutAmountStrictZero(t *testing.T) {
	payload := decodeFixture(t, `{"currency":"eur","total_summary":{"due":0},"invoice":{"amount_due":0}}`)
	amount, currency, ok := checkoutAmount(payload)
	if !ok || amount != 0 || currency != "eur" {
		t.Fatalf("unexpected amount result: %d %s %v", amount, currency, ok)
	}
	payload["total_summary"] = map[string]any{"due": json.Number("2300")}
	amount, _, ok = checkoutAmount(payload)
	if !ok || amount != 2300 {
		t.Fatalf("non-zero amount not detected: %d %v", amount, ok)
	}
}

func TestPayPalMethodMismatchDetection(t *testing.T) {
	payload := decodeFixture(t, `{"error":{"code":"checkout_confirm_error","extra_fields":{"confirm_error_reason":"payment_method_types_mismatch","payment_method_type":"paypal"}}}`)
	if !isPayPalMethodMismatch(payload) {
		t.Fatal("PayPal method mismatch was not detected")
	}
	payload["error"].(map[string]any)["extra_fields"].(map[string]any)["payment_method_type"] = "card"
	if isPayPalMethodMismatch(payload) {
		t.Fatal("card method mismatch was classified as PayPal unavailable")
	}
}

func TestRedirectAndSubmissionExtraction(t *testing.T) {
	payload := map[string]any{
		"submission_attempt": map[string]any{"state": "requires_approval"},
		"setup_intent": map[string]any{
			"next_action": map[string]any{
				"redirect_to_url": map[string]any{"url": "https://pm-redirects.stripe.com/authorize/test"},
			},
		},
	}
	if submissionState(payload) != "requires_approval" {
		t.Fatal("submission state missing")
	}
	if redirectURL(payload) != "https://pm-redirects.stripe.com/authorize/test" {
		t.Fatal("redirect missing")
	}
}

func TestPaymentFailureExtractionPrefersDeclineCode(t *testing.T) {
	payload := decodeFixture(t, `{
        "submission_attempt": {
            "state": "failed",
            "error": {"code": "checkout_approval_payment_failure_with_payment_error", "payment_error": {"code": "setup_attempt_failed", "decline_code": "generic_decline"}}
        },
        "setup_intent": {"status": "requires_payment_method", "last_setup_error": {"code": "setup_attempt_failed", "decline_code": "generic_decline"}}
    }`)
	if got := paymentFailure(payload); got != "generic_decline" {
		t.Fatalf("payment failure = %q", got)
	}
}

func TestNormalizeProxyAndInput(t *testing.T) {
	if normalizeProxy("socks5h://user:pass@example.com:1080") != "socks5://user:pass@example.com:1080" {
		t.Fatal("socks5h was not normalized")
	}
	if requireInput(Input{SessionID: "cs_live_test", PublishableKey: "pk_live_test", ProxyURL: "socks5://proxy:1"}) != nil {
		t.Fatal("valid input rejected")
	}
}
