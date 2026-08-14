package stripeworker

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"net/url"
	"strings"
	"time"

	http "github.com/bogdanfinn/fhttp"
)

func Run(ctx context.Context, input Input) Output {
	w, err := newWorker(input)
	if err != nil {
		return Output{OK: false, Code: "go_worker_input_error", Message: err.Error()}
	}
	defer w.close()
	redirect, err := w.run(ctx)
	if err != nil {
		code := "go_stripe_failed"
		if typed, ok := err.(*workerError); ok && typed.code != "" {
			code = typed.code
		}
		return Output{OK: false, Code: code, Message: err.Error(), Diagnostics: w.diagnostics}
	}
	return Output{OK: true, Code: "paypal_redirect_extracted", RedirectURL: redirect, Diagnostics: w.diagnostics}
}

func (w *worker) run(ctx context.Context) (string, error) {
	init, version, err := w.initCheckout(ctx)
	if err != nil {
		return "", err
	}
	if err := requirePayPal(init); err != nil {
		return "", err
	}

	latest := cloneMap(init)
	contextValues := newCheckoutContext(latest)
	elements, err := w.elementsSession(ctx, latest, contextValues, version)
	if err != nil {
		return "", err
	}
	mergeUseful(latest, elements)
	contextValues.update(latest)

	amount, _, known := checkoutAmount(init)
	if !known {
		return "", &workerError{code: "go_stripe_amount_unknown", message: "Go Stripe 未返回可核验的应付金额"}
	}
	if amount != 0 {
		if !w.input.ApplyPromo {
			return "", &workerError{code: "non_zero_amount", message: fmt.Sprintf("Go Stripe 前置优惠未生效（应付金额 %d）", amount)}
		}
		if err := w.applyPromo(ctx); err != nil {
			return "", err
		}
		var synced bool
		for attempt := 0; attempt < 6; attempt++ {
			delay := 1500 * time.Millisecond
			if attempt == 0 {
				delay = 800 * time.Millisecond
			}
			select {
			case <-ctx.Done():
				return "", &workerError{code: "go_stripe_timeout", message: ctx.Err().Error()}
			case <-time.After(delay):
			}
			refreshed, refreshedVersion, refreshErr := w.initCheckout(ctx)
			if refreshErr != nil {
				return "", refreshErr
			}
			if err := requirePayPal(refreshed); err != nil {
				return "", err
			}
			latest = cloneMap(refreshed)
			version = refreshedVersion
			contextValues.update(latest)
			if refreshedAmount, _, ok := checkoutAmount(latest); ok && refreshedAmount == 0 {
				synced = true
				break
			}
		}
		if !synced {
			if err := strictZero(latest); err != nil {
				return "", err
			}
		}
		elements, err = w.elementsSession(ctx, latest, contextValues, version)
		if err != nil {
			return "", err
		}
		mergeUseful(latest, elements)
		contextValues.update(latest)
	}
	if err := strictZero(latest); err != nil {
		return "", err
	}

	tax, err := w.updateTax(ctx, contextValues, version)
	if err != nil {
		return "", err
	}
	if err := strictZero(tax); err != nil {
		return "", err
	}
	mergeUseful(latest, tax)
	contextValues.update(latest)
	if err := strictZero(latest); err != nil {
		return "", err
	}
	w.snapshotBilling(ctx)
	paymentMethod, err := w.createPayPalPaymentMethod(ctx, contextValues)
	if err != nil {
		return "", err
	}
	confirmed, err := w.confirm(ctx, latest, contextValues, paymentMethod)
	if err != nil {
		return "", err
	}
	if redirect := redirectURL(confirmed); redirect != "" {
		return redirect, nil
	}
	if submissionState(confirmed) == "requires_approval" {
		if redirect, _ := w.pollRedirect(ctx, 1, "pre-approve"); redirect != "" {
			return redirect, nil
		}
		if err := w.approve(ctx); err != nil {
			return "", err
		}
		return w.pollRedirect(ctx, 5, "post-approve")
	}
	return w.pollRedirect(ctx, 5, "post-confirm")
}

func (w *worker) initCheckout(ctx context.Context) (map[string]any, string, error) {
	endpoint := strings.TrimRight(w.input.StripeBase, "/") + "/v1/payment_pages/" + w.input.SessionID + "/init"
	locale, timezone := w.localeProfile()
	forms := []struct {
		version string
		form    url.Values
	}{
		{stripeVersionBase, url.Values{"key": {w.input.PublishableKey}, "eid": {"NA"}, "browser_locale": {locale}, "browser_timezone": {timezone}, "redirect_type": {"url"}}},
		{stripeVersionFull, url.Values{
			"key": {w.input.PublishableKey}, "_stripe_version": {stripeVersionFull}, "browser_locale": {locale}, "browser_timezone": {timezone},
			"elements_session_client[elements_init_source]": {"custom_checkout"}, "elements_session_client[referrer_host]": {"chatgpt.com"},
			"elements_session_client[stripe_js_id]": {randomID(16)}, "elements_session_client[locale]": {"en-US"},
			"elements_session_client[is_aggregation_expected]": {"false"}, "elements_session_client[client_betas][0]": {"custom_checkout_server_updates_1"},
			"elements_session_client[client_betas][1]":                   {"custom_checkout_manual_approval_1"},
			"elements_options_client[saved_payment_method][enable_save]": {"never"}, "elements_options_client[saved_payment_method][enable_redisplay]": {"never"},
		}},
	}
	var last error
	for _, candidate := range forms {
		payload, _, err := w.requestForm(ctx, w.stripe, http.MethodPost, endpoint, candidate.form, stripeHeaders(), "go_stripe_init", "/v1/payment_pages/{session}/init")
		if err == nil {
			return payload, candidate.version, nil
		}
		last = err
	}
	return nil, "", last
}

func strictZero(payload map[string]any) error {
	amount, currency, ok := checkoutAmount(payload)
	if !ok {
		return &workerError{code: "go_stripe_amount_unknown", message: "Go Stripe 未返回可核验的应付金额"}
	}
	if amount != 0 {
		return &workerError{code: "non_zero_amount", message: fmt.Sprintf("Go Stripe 后置优惠未同步至 0 元（应付金额 %d %s）", amount, strings.ToUpper(currency))}
	}
	return nil
}

func requirePayPal(payload map[string]any) error {
	methods, explicit := paymentMethodTypes(payload)
	if !explicit || len(methods) == 0 {
		methods = []string{"card"}
	}
	if !hasString(methods, "paypal") {
		return &workerError{code: "paypal_unavailable", message: fmt.Sprintf("Stripe init 未开放 PayPal（methods=%s）", strings.Join(uniqueStrings(methods...), ","))}
	}
	return nil
}

type checkoutContext struct {
	stripeJSID     string
	elementsID     string
	elementsConfig string
	clientSession  string
	guid           string
	muid           string
	sid            string
	configID       string
	initChecksum   string
	amount         string
	currency       string
	locale         string
	uiMode         string
}

func newCheckoutContext(payload map[string]any) *checkoutContext {
	ctx := &checkoutContext{
		stripeJSID:    randomUUID(),
		elementsID:    "elements_session_" + randomID(6),
		clientSession: randomUUID(),
		guid:          randomUUID(),
		muid:          randomUUID(),
		sid:           randomUUID(),
		locale:        "en",
		uiMode:        stringValue(payload["ui_mode"]),
	}
	ctx.update(payload)
	return ctx
}

func (ctx *checkoutContext) update(payload map[string]any) {
	if payload == nil {
		return
	}
	if value := stringValue(payload["config_id"]); value != "" {
		ctx.configID = value
	}
	if value := stringValue(payload["init_checksum"]); value != "" {
		ctx.initChecksum = value
	}
	if value := stringValue(payload["locale"]); value != "" {
		ctx.locale = strings.Split(value, "-")[0]
	}
	if value := stringValue(payload["ui_mode"]); value != "" {
		ctx.uiMode = value
	}
	if amount, currency, ok := checkoutAmount(payload); ok {
		ctx.amount = fmt.Sprint(amount)
		if currency != "" {
			ctx.currency = currency
		}
	}
}

func (w *worker) elementsSession(ctx context.Context, init map[string]any, checkout *checkoutContext, version string) (map[string]any, error) {
	form := url.Values{
		"client_betas[0]": {"custom_checkout_server_updates_1"}, "client_betas[1]": {"custom_checkout_manual_approval_1"},
		"deferred_intent[mode]": {"subscription"}, "deferred_intent[amount]": {checkout.amount}, "deferred_intent[currency]": {checkout.currency},
		"deferred_intent[setup_future_usage]": {"off_session"}, "currency": {checkout.currency}, "key": {w.input.PublishableKey}, "_stripe_version": {version},
		"elements_init_source": {"custom_checkout"}, "referrer_host": {"chatgpt.com"}, "stripe_js_id": {checkout.stripeJSID}, "locale": {checkout.locale},
		"type": {"deferred_intent"}, "checkout_session_id": {w.input.SessionID},
	}
	methods, _ := paymentMethodTypes(init)
	if len(methods) == 0 {
		methods = []string{"card", "paypal"}
	}
	for index, method := range methods {
		form.Set(fmt.Sprintf("deferred_intent[payment_method_types][%d]", index), method)
	}
	endpoint := strings.TrimRight(w.input.StripeBase, "/") + "/v1/elements/sessions"
	payload, _, err := w.requestForm(ctx, w.stripe, http.MethodGet, endpoint, form, stripeHeaders(), "go_stripe_elements", "/v1/elements/sessions")
	if payload != nil {
		if value := stringValue(payload["session_id"]); value != "" {
			checkout.elementsID = value
		}
		if value := stringValue(payload["config_id"]); value != "" {
			checkout.elementsConfig = value
		}
	}
	return payload, err
}

func (w *worker) updateTax(ctx context.Context, checkout *checkoutContext, version string) (map[string]any, error) {
	address := w.input.Billing.Address
	form := url.Values{
		"elements_session_client[client_betas][0]": {"custom_checkout_server_updates_1"}, "elements_session_client[client_betas][1]": {"custom_checkout_manual_approval_1"},
		"elements_session_client[elements_init_source]": {"custom_checkout"}, "elements_session_client[referrer_host]": {"chatgpt.com"},
		"elements_session_client[stripe_js_id]": {checkout.stripeJSID}, "elements_session_client[session_id]": {checkout.elementsID},
		"elements_session_client[locale]": {checkout.locale}, "elements_session_client[is_aggregation_expected]": {"false"},
		"key": {w.input.PublishableKey}, "_stripe_version": {version},
		"elements_options_client[saved_payment_method][enable_save]": {"never"}, "elements_options_client[saved_payment_method][enable_redisplay]": {"never"},
	}
	setNonEmpty(form, "tax_region[country]", address.Country)
	setNonEmpty(form, "tax_region[line1]", address.Line1)
	setNonEmpty(form, "tax_region[city]", address.City)
	setNonEmpty(form, "tax_region[postal_code]", address.PostalCode)
	setNonEmpty(form, "tax_region[state]", address.State)
	endpoint := strings.TrimRight(w.input.StripeBase, "/") + "/v1/payment_pages/" + w.input.SessionID
	payload, _, err := w.requestForm(ctx, w.stripe, http.MethodPost, endpoint, form, stripeHeaders(), "go_stripe_tax", "/v1/payment_pages/{session}")
	return payload, err
}

func (w *worker) applyPromo(ctx context.Context) error {
	route := "/backend-api/payments/checkout/update"
	payload := map[string]any{
		"checkout_session_id": w.input.SessionID,
		"processor_entity":    w.input.ProcessorEntity,
		"plan_name":           "chatgptplusplan",
		"price_interval":      "month",
		"seat_quantity":       1,
		"discount_code":       nil,
		"promo_campaign": map[string]any{
			"promo_campaign_id":          "plus-1-month-free",
			"is_coupon_from_query_param": false,
		},
	}
	_, status, err := w.requestJSON(
		ctx,
		w.chatGPT,
		http.MethodPost,
		strings.TrimRight(w.input.ChatGPTBase, "/")+route,
		payload,
		w.promoHeaders(route),
		"go_checkout_promo_update",
		route,
	)
	if err != nil {
		return &workerError{
			code:    "promo_update_failed",
			message: fmt.Sprintf("Go Stripe 后置优惠更新失败 HTTP %d: %v", status, err),
		}
	}
	return nil
}

func (w *worker) snapshotBilling(ctx context.Context) {
	route := "/backend-api/payments/checkout/snapshot"
	address := map[string]any{
		"line1":       w.input.Billing.Address.Line1,
		"city":        w.input.Billing.Address.City,
		"country":     w.input.Billing.Address.Country,
		"postal_code": w.input.Billing.Address.PostalCode,
	}
	if state := strings.TrimSpace(w.input.Billing.Address.State); state != "" {
		address["state"] = state
	}
	payload := map[string]any{
		"snapshot": map[string]any{
			"billing_address": map[string]any{
				"name":    w.input.Billing.Name,
				"address": address,
			},
		},
	}
	// The reference flow treats snapshot as best-effort: Stripe confirmation is
	// still authoritative if this auxiliary ChatGPT request is unavailable.
	_, _, _ = w.requestJSON(
		ctx,
		w.chatGPT,
		http.MethodPost,
		strings.TrimRight(w.input.ChatGPTBase, "/")+route,
		payload,
		w.snapshotHeaders(),
		"go_checkout_snapshot",
		route,
	)
}

func (w *worker) createPayPalPaymentMethod(ctx context.Context, checkout *checkoutContext) (string, error) {
	address := w.input.Billing.Address
	form := url.Values{
		"type":                   {"paypal"},
		"billing_details[name]":  {w.input.Billing.Name},
		"billing_details[email]": {w.input.Billing.Email},
		"payment_user_agent":     {fmt.Sprintf("stripe.js/%s; stripe-js-v3/%s; payment-element; deferred-intent", stripeRuntime, stripeRuntime)},
		"referrer":               {"https://chatgpt.com"},
		"time_on_page":           {"30000"},
		"client_attribution_metadata[client_session_id]":                           {checkout.stripeJSID},
		"client_attribution_metadata[checkout_session_id]":                         {w.input.SessionID},
		"client_attribution_metadata[checkout_config_id]":                          {checkout.configID},
		"client_attribution_metadata[elements_session_id]":                         {checkout.elementsID},
		"client_attribution_metadata[elements_session_config_id]":                  {checkout.elementsConfig},
		"client_attribution_metadata[merchant_integration_source]":                 {"elements"},
		"client_attribution_metadata[merchant_integration_subtype]":                {"payment-element"},
		"client_attribution_metadata[merchant_integration_version]":                {"2021"},
		"client_attribution_metadata[payment_intent_creation_flow]":                {"deferred"},
		"client_attribution_metadata[payment_method_selection_flow]":               {"automatic"},
		"client_attribution_metadata[merchant_integration_additional_elements][0]": {"payment"},
		"client_attribution_metadata[merchant_integration_additional_elements][1]": {"address"},
		"guid":            {checkout.guid},
		"muid":            {checkout.muid},
		"sid":             {checkout.sid},
		"key":             {w.input.PublishableKey},
		"_stripe_version": {stripeVersionBase},
	}
	setNonEmpty(form, "billing_details[address][country]", address.Country)
	setNonEmpty(form, "billing_details[address][line1]", address.Line1)
	setNonEmpty(form, "billing_details[address][city]", address.City)
	setNonEmpty(form, "billing_details[address][postal_code]", address.PostalCode)
	setNonEmpty(form, "billing_details[address][state]", address.State)

	endpoint := strings.TrimRight(w.input.StripeBase, "/") + "/v1/payment_methods"
	payload, _, err := w.requestForm(ctx, w.stripe, http.MethodPost, endpoint, form, stripeHeaders(), "go_stripe_payment_method", "/v1/payment_methods")
	if err != nil {
		return "", err
	}
	paymentMethod := stringValue(payload["id"])
	if !strings.HasPrefix(paymentMethod, "pm_") {
		return "", &workerError{code: "go_stripe_payment_method_invalid", message: "Go Stripe 未返回有效的 PayPal pm_*"}
	}
	return paymentMethod, nil
}

func (w *worker) confirm(ctx context.Context, init map[string]any, checkout *checkoutContext, paymentMethod string) (map[string]any, error) {
	form := w.confirmForm(init, checkout, paymentMethod)
	endpoint := strings.TrimRight(w.input.StripeBase, "/") + "/v1/payment_pages/" + w.input.SessionID + "/confirm"
	payload, _, err := w.requestForm(ctx, w.stripe, http.MethodPost, endpoint, form, stripeHeaders(), "go_stripe_confirm", "/v1/payment_pages/{session}/confirm")
	if err != nil && isPayPalMethodMismatch(payload) {
		return payload, &workerError{
			code:    "paypal_unavailable",
			message: "Stripe confirm 明确拒绝 PayPal（payment_method_types_mismatch）",
		}
	}
	return payload, err
}

func (w *worker) confirmForm(init map[string]any, checkout *checkoutContext, paymentMethod string) url.Values {
	returnURL := w.returnURL(init)
	form := url.Values{
		"eid":                          {"NA"},
		"payment_method":               {paymentMethod},
		"expected_amount":              {checkout.amount},
		"expected_payment_method_type": {"paypal"},
		"return_url":                   {returnURL},
		"_stripe_version":              {payPalStripeVersion},
		"guid":                         {checkout.guid},
		"muid":                         {checkout.muid},
		"sid":                          {checkout.sid},
		"key":                          {w.input.PublishableKey},
		"version":                      {stripeRuntime},
		"init_checksum":                {checkout.initChecksum},
		"client_attribution_metadata[client_session_id]":             {checkout.clientSession},
		"client_attribution_metadata[checkout_session_id]":           {w.input.SessionID},
		"client_attribution_metadata[merchant_integration_source]":   {"checkout"},
		"client_attribution_metadata[merchant_integration_version]":  {"custom_checkout"},
		"client_attribution_metadata[payment_method_selection_flow]": {"automatic"},
		"client_attribution_metadata[checkout_config_id]":            {checkout.configID},
		"link_brand": {"link"},
	}
	return form
}

func setNonEmpty(form url.Values, key, value string) {
	if value = strings.TrimSpace(value); value != "" {
		form.Set(key, value)
	}
}

func (w *worker) returnURL(init map[string]any) string {
	hosted := stringValue(init["stripe_hosted_url"])
	if hosted == "" {
		hosted = "https://checkout.stripe.com/c/pay/" + w.input.SessionID
	}
	if !strings.Contains(hosted, "/c/pay/") {
		hosted = "https://checkout.stripe.com/c/pay/" + w.input.SessionID
	}
	hosted = strings.Replace(hosted, "https://pay.openai.com/", "https://checkout.stripe.com/", 1)
	parsed, err := url.Parse(hosted)
	if err != nil {
		return hosted
	}
	query := parsed.Query()
	query.Set("redirect_pm_type", "paypal")
	query.Set("lid", randomUUID())
	query.Set("ui_mode", "custom")
	parsed.RawQuery = query.Encode()
	return parsed.String()
}

func (w *worker) approve(ctx context.Context) error {
	if w.input.AccessToken == "" || w.input.ApproveHeaders["OpenAI-Sentinel-Token"] == "" {
		return &workerError{code: "go_approve_context_missing", message: "Go Stripe approve 缺少 Access Token 或 Sentinel"}
	}
	route := "/backend-api/payments/checkout/approve"
	payload := map[string]any{"checkout_session_id": w.input.SessionID, "processor_entity": w.input.ProcessorEntity}
	response, _, err := w.requestJSON(ctx, w.chatGPT, http.MethodPost, strings.TrimRight(w.input.ChatGPTBase, "/")+route, payload, w.chatGPTHeaders(route), "go_chatgpt_approve", route)
	if err != nil {
		return err
	}
	result := strings.ToLower(stringValue(response["result"]))
	if result != "approved" {
		return &workerError{code: "go_approve_failed", message: "Go Stripe approve 未通过: result=" + result}
	}
	return nil
}

func (w *worker) pollRedirect(ctx context.Context, maxAttempts int, stage string) (string, error) {
	for attempt := 0; attempt < maxAttempts; attempt++ {
		if attempt == 0 {
			endpoint := strings.TrimRight(w.input.StripeBase, "/") + "/v1/payment_pages/" + w.input.SessionID + "/poll"
			form := url.Values{"key": {w.input.PublishableKey}, "_stripe_version": {stripeVersionBase}}
			payload, _, err := w.requestForm(ctx, w.stripe, http.MethodGet, endpoint, form, stripeHeaders(), "go_stripe_poll_light", "/v1/payment_pages/{session}/poll")
			if err == nil {
				if redirect := redirectURL(payload); redirect != "" {
					return redirect, nil
				}
			}
		}
		form := url.Values{
			"key": {w.input.PublishableKey}, "_stripe_version": {stripeVersionFull},
			"elements_session_client[client_betas][0]": {"custom_checkout_server_updates_1"}, "elements_session_client[client_betas][1]": {"custom_checkout_manual_approval_1"},
			"elements_session_client[elements_init_source]": {"custom_checkout"}, "elements_session_client[referrer_host]": {"chatgpt.com"},
		}
		endpoint := strings.TrimRight(w.input.StripeBase, "/") + "/v1/payment_pages/" + w.input.SessionID
		payload, _, err := w.requestForm(ctx, w.stripe, http.MethodGet, endpoint, form, stripeHeaders(), "go_stripe_poll", "/v1/payment_pages/{session}")
		if err == nil {
			if redirect := redirectURL(payload); redirect != "" {
				return redirect, nil
			}
			if decline := paymentFailure(payload); decline != "" {
				return "", &workerError{
					code:    "paypal_setup_declined",
					message: "Go Stripe PayPal setup declined: " + decline,
				}
			}
		}
		if attempt+1 >= maxAttempts {
			break
		}
		select {
		case <-ctx.Done():
			return "", &workerError{code: "go_stripe_timeout", message: ctx.Err().Error()}
		case <-time.After(time.Second):
		}
	}
	return "", &workerError{code: "go_stripe_redirect_missing", message: "Go Stripe " + stage + " 未返回 PayPal redirect"}
}

func cloneMap(source map[string]any) map[string]any {
	out := make(map[string]any, len(source))
	for key, value := range source {
		out[key] = value
	}
	return out
}

func mergeUseful(destination, source map[string]any) {
	if source == nil {
		return
	}
	for _, key := range []string{"init_checksum", "total_summary", "invoice", "line_item_group", "elements_options", "payment_intent", "payment_method_types", "payment_method_specs", "config_id", "locale", "currency", "ui_mode", "return_url", "stripe_hosted_url"} {
		if source[key] != nil {
			destination[key] = source[key]
		}
	}
}

func randomID(bytesLength int) string {
	buffer := make([]byte, bytesLength)
	if _, err := rand.Read(buffer); err != nil {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(buffer)
}

func randomUUID() string {
	raw := randomID(16)
	if len(raw) != 32 {
		return raw
	}
	return raw[0:8] + "-" + raw[8:12] + "-" + raw[12:16] + "-" + raw[16:20] + "-" + raw[20:32]
}

func localeForCountry(country string) (string, string) {
	profiles := map[string][2]string{
		"BR": {"pt-BR", "America/Sao_Paulo"},
		"DE": {"de-DE", "Europe/Berlin"},
		"FR": {"fr-FR", "Europe/Paris"},
		"GB": {"en-GB", "Europe/London"},
		"JP": {"ja-JP", "Asia/Tokyo"},
		"US": {"en-US", "America/New_York"},
	}
	if profile, ok := profiles[strings.ToUpper(strings.TrimSpace(country))]; ok {
		return profile[0], profile[1]
	}
	return "en-US", "UTC"
}

func (w *worker) localeProfile() (string, string) {
	locale := strings.TrimSpace(w.input.BrowserLocale)
	timezone := strings.TrimSpace(w.input.BrowserTimezone)
	fallbackLocale, fallbackTimezone := localeForCountry(w.input.Country)
	if locale == "" {
		locale = fallbackLocale
	}
	if timezone == "" {
		timezone = fallbackTimezone
	}
	return locale, timezone
}
