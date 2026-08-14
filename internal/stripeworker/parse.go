package stripeworker

import (
	"encoding/json"
	"fmt"
	"net/url"
	"strconv"
	"strings"
)

func mapValue(root map[string]any, keys ...string) any {
	var current any = root
	for _, key := range keys {
		next, ok := current.(map[string]any)
		if !ok {
			return nil
		}
		current = next[key]
	}
	return current
}

func stringValue(value any) string {
	switch item := value.(type) {
	case string:
		return strings.TrimSpace(item)
	case json.Number:
		return item.String()
	case float64:
		return strconv.FormatFloat(item, 'f', -1, 64)
	case int:
		return strconv.Itoa(item)
	case int64:
		return strconv.FormatInt(item, 10)
	default:
		return ""
	}
}

func int64Value(value any) (int64, bool) {
	switch item := value.(type) {
	case json.Number:
		parsed, err := item.Int64()
		return parsed, err == nil
	case float64:
		if item != float64(int64(item)) {
			return 0, false
		}
		return int64(item), true
	case int:
		return int64(item), true
	case int64:
		return item, true
	case string:
		parsed, err := strconv.ParseInt(strings.TrimSpace(item), 10, 64)
		return parsed, err == nil
	default:
		return 0, false
	}
}

func checkoutAmount(payload map[string]any) (int64, string, bool) {
	currency := strings.ToLower(stringValue(payload["currency"]))
	paths := [][]string{
		{"total_summary", "due"},
		{"total_summary", "total"},
		{"invoice", "amount_due"},
		{"invoice", "total"},
		{"elements_options", "amount"},
		{"payment_intent", "amount"},
	}
	found := false
	for _, path := range paths {
		value := mapValue(payload, path...)
		if value == nil {
			continue
		}
		amount, ok := int64Value(value)
		if !ok {
			return 0, currency, false
		}
		found = true
		if amount != 0 {
			return amount, currency, true
		}
	}
	return 0, currency, found
}

func paymentMethodTypes(payload map[string]any) ([]string, bool) {
	values := make([]string, 0)
	explicit := false
	if raw, ok := payload["payment_method_types"].([]any); ok {
		explicit = true
		for _, value := range raw {
			if item := strings.ToLower(stringValue(value)); item != "" {
				values = append(values, item)
			}
		}
	}
	if raw, ok := payload["payment_method_specs"].([]any); ok {
		explicit = true
		for _, value := range raw {
			if item, ok := value.(map[string]any); ok {
				if kind := strings.ToLower(stringValue(item["type"])); kind != "" {
					values = append(values, kind)
				}
			}
		}
	}
	return uniqueStrings(values...), explicit
}

func isPayPalMethodMismatch(payload map[string]any) bool {
	reason := strings.ToLower(stringValue(mapValue(payload, "error", "extra_fields", "confirm_error_reason")))
	method := strings.ToLower(stringValue(mapValue(payload, "error", "extra_fields", "payment_method_type")))
	return reason == "payment_method_types_mismatch" && method == "paypal"
}

func hasString(values []string, expected string) bool {
	for _, value := range values {
		if strings.EqualFold(value, expected) {
			return true
		}
	}
	return false
}

func uniqueStrings(values ...string) []string {
	seen := map[string]bool{}
	out := make([]string, 0, len(values))
	for _, value := range values {
		value = strings.TrimSpace(value)
		if value != "" && !seen[value] {
			seen[value] = true
			out = append(out, value)
		}
	}
	return out
}

func redirectURL(payload map[string]any) string {
	if next, ok := payload["next_action"].(map[string]any); ok {
		if redirect, ok := next["redirect_to_url"].(map[string]any); ok {
			if raw := stringValue(redirect["url"]); isPayPalRedirect(raw) {
				return raw
			}
		}
	}
	for _, key := range []string{"payment_intent", "setup_intent", "payment_method_object"} {
		if nested, ok := payload[key].(map[string]any); ok {
			if raw := redirectURL(nested); raw != "" {
				return raw
			}
		}
	}
	raw, _ := json.Marshal(payload)
	text := strings.ReplaceAll(string(raw), `\/`, `/`)
	for _, prefix := range []string{"https://pm-redirects.stripe.com/authorize/", "https://www.paypal.com/agreements/approve?"} {
		if index := strings.Index(text, prefix); index >= 0 {
			end := index
			for end < len(text) && !strings.ContainsRune(`\"'<> `, rune(text[end])) {
				end++
			}
			return strings.ReplaceAll(text[index:end], `\u0026`, "&")
		}
	}
	return ""
}

func isPayPalRedirect(raw string) bool {
	parsed, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || parsed.Hostname() == "" {
		return false
	}
	host := strings.ToLower(parsed.Hostname())
	return strings.Contains(host, "paypal.com") ||
		(strings.HasSuffix(host, "stripe.com") && strings.Contains(strings.ToLower(parsed.Path), "authorize"))
}

func submissionState(payload map[string]any) string {
	if submission, ok := payload["submission_attempt"].(map[string]any); ok {
		return strings.ToLower(stringValue(submission["state"]))
	}
	for _, value := range payload {
		if nested, ok := value.(map[string]any); ok {
			if state := submissionState(nested); state != "" {
				return state
			}
		}
	}
	return ""
}

func paymentFailure(payload map[string]any) string {
	paths := [][]string{
		{"submission_attempt", "error", "payment_error", "decline_code"},
		{"setup_intent", "last_setup_error", "decline_code"},
		{"payment_intent", "last_payment_error", "decline_code"},
		{"submission_attempt", "error", "payment_error", "code"},
		{"setup_intent", "last_setup_error", "code"},
		{"payment_intent", "last_payment_error", "code"},
		{"submission_attempt", "error", "code"},
	}
	for _, path := range paths {
		if value := strings.ToLower(stringValue(mapValue(payload, path...))); value != "" {
			return value
		}
	}
	return ""
}

func normalizeProxy(raw string) string {
	if strings.HasPrefix(strings.ToLower(raw), "socks5h://") {
		return "socks5://" + raw[len("socks5h://"):]
	}
	return raw
}

func requireInput(input Input) error {
	if !strings.HasPrefix(input.SessionID, "cs_live_") && !strings.HasPrefix(input.SessionID, "cs_test_") {
		return fmt.Errorf("invalid Stripe Checkout session")
	}
	if !strings.HasPrefix(input.PublishableKey, "pk_live_") && !strings.HasPrefix(input.PublishableKey, "pk_test_") {
		return fmt.Errorf("invalid Stripe publishable key")
	}
	if strings.TrimSpace(input.ProxyURL) == "" {
		return fmt.Errorf("missing proxy")
	}
	return nil
}
