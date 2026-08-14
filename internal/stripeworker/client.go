package stripeworker

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"strings"
	"time"

	http "github.com/bogdanfinn/fhttp"
	tlsclient "github.com/bogdanfinn/tls-client"
	"github.com/bogdanfinn/tls-client/profiles"
)

type httpClient interface {
	Do(*http.Request) (*http.Response, error)
	CloseIdleConnections()
}

type worker struct {
	input       Input
	stripe      httpClient
	chatGPT     httpClient
	diagnostics []Diagnostic
}

func newTLSClient(proxyURL string) (httpClient, error) {
	options := []tlsclient.HttpClientOption{
		tlsclient.WithTimeoutSeconds(defaultHTTPTimeout),
		tlsclient.WithClientProfile(profiles.Firefox_147),
		tlsclient.WithCookieJar(tlsclient.NewCookieJar()),
		tlsclient.WithNotFollowRedirects(),
	}
	if proxyURL = normalizeProxy(strings.TrimSpace(proxyURL)); proxyURL != "" {
		options = append(options, tlsclient.WithProxyUrl(proxyURL))
	}
	return tlsclient.NewHttpClient(tlsclient.NewNoopLogger(), options...)
}

func newWorker(input Input) (*worker, error) {
	if err := requireInput(input); err != nil {
		return nil, err
	}
	stripe, err := newTLSClient(input.ProxyURL)
	if err != nil {
		return nil, err
	}
	chatGPT, err := newTLSClient(input.ProxyURL)
	if err != nil {
		stripe.CloseIdleConnections()
		return nil, err
	}
	if input.StripeBase == "" {
		input.StripeBase = stripeAPI
	}
	if input.ChatGPTBase == "" {
		input.ChatGPTBase = chatGPTBase
	}
	return &worker{input: input, stripe: stripe, chatGPT: chatGPT}, nil
}

func (w *worker) close() {
	w.stripe.CloseIdleConnections()
	w.chatGPT.CloseIdleConnections()
}

func (w *worker) requestForm(ctx context.Context, client httpClient, method, endpoint string, form url.Values, headers map[string]string, kind, route string) (map[string]any, int, error) {
	var body io.Reader
	if method == http.MethodGet {
		separator := "?"
		if strings.Contains(endpoint, "?") {
			separator = "&"
		}
		endpoint += separator + form.Encode()
	} else {
		body = strings.NewReader(form.Encode())
	}
	return w.request(ctx, client, method, endpoint, body, headers, w.formSummary(form), kind, route)
}

func (w *worker) requestJSON(ctx context.Context, client httpClient, method, endpoint string, payload any, headers map[string]string, kind, route string) (map[string]any, int, error) {
	raw, err := json.Marshal(payload)
	if err != nil {
		return nil, 0, err
	}
	return w.request(ctx, client, method, endpoint, bytes.NewReader(raw), headers, payload, kind, route)
}

func (w *worker) request(ctx context.Context, client httpClient, method, endpoint string, body io.Reader, headers map[string]string, requestSummary any, kind, route string) (map[string]any, int, error) {
	requestSummary = w.sanitizeDiagnosticValue(requestSummary, "")
	req, err := http.NewRequestWithContext(ctx, method, endpoint, body)
	if err != nil {
		return nil, 0, err
	}
	for key, value := range headers {
		if value != "" {
			req.Header.Set(key, value)
		}
	}
	resp, err := client.Do(req)
	if err != nil {
		w.diagnostics = append(w.diagnostics, Diagnostic{Kind: kind, Method: method, Route: route, Request: requestSummary, Error: w.scrubDiagnosticString(err.Error())})
		return nil, 0, &workerError{code: kind + "_network_error", message: fmt.Sprintf("%s network error: %v", kind, err)}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, maxResponseBytes))
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var payload map[string]any
	if err := decoder.Decode(&payload); err != nil {
		w.diagnostics = append(w.diagnostics, Diagnostic{Kind: kind, Method: method, Route: route, HTTPStatus: resp.StatusCode, Request: requestSummary})
		return nil, resp.StatusCode, &workerError{code: kind + "_invalid_json", message: fmt.Sprintf("%s returned non-JSON HTTP %d", kind, resp.StatusCode)}
	}
	diagnostic := Diagnostic{Kind: kind, Method: method, Route: route, HTTPStatus: resp.StatusCode, Request: requestSummary}
	if sanitized, marshalErr := json.Marshal(w.sanitizeDiagnosticValue(payload, "")); marshalErr == nil {
		diagnostic.Response = sanitized
	}
	w.diagnostics = append(w.diagnostics, diagnostic)
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return payload, resp.StatusCode, &workerError{code: kind + "_failed", message: fmt.Sprintf("%s failed HTTP %d: %s", kind, resp.StatusCode, stripeError(payload))}
	}
	return payload, resp.StatusCode, nil
}

func stripeHeaders() map[string]string {
	return map[string]string{
		"User-Agent":   stripeUserAgent,
		"Accept":       "application/json",
		"Content-Type": "application/x-www-form-urlencoded",
		"Origin":       "https://js.stripe.com",
		"Referer":      "https://js.stripe.com/",
	}
}

func (w *worker) chatGPTHeaders(route string) map[string]string {
	locale, _ := w.localeProfile()
	language := strings.Split(locale, "-")[0]
	return map[string]string{
		"User-Agent":               stripeUserAgent,
		"Accept":                   "*/*",
		"Content-Type":             "application/json",
		"Authorization":            "Bearer " + w.input.AccessToken,
		"Cookie":                   w.input.CookieHeader,
		"Origin":                   "https://chatgpt.com",
		"Referer":                  fmt.Sprintf("https://chatgpt.com/checkout/%s/%s", w.input.ProcessorEntity, w.input.SessionID),
		"OAI-Device-ID":            w.input.DeviceID,
		"X-OpenAI-Target-Path":     route,
		"X-OpenAI-Target-Route":    route,
		"OpenAI-Sentinel-Token":    w.input.ApproveHeaders["OpenAI-Sentinel-Token"],
		"OpenAI-Sentinel-SO-Token": firstHeader(w.input.ApproveHeaders, "OpenAI-Sentinel-SO-Token", "OpenAI-Sentinel-So-Token"),
		"OAI-Telemetry":            w.input.ApproveHeaders["OAI-Telemetry"],
		"Accept-Language":          locale + "," + language + ";q=0.9",
		"Sec-Fetch-Dest":           "empty",
		"Sec-Fetch-Mode":           "cors",
		"Sec-Fetch-Site":           "same-origin",
	}
}

func (w *worker) promoHeaders(route string) map[string]string {
	headers := w.chatGPTHeaders(route)
	delete(headers, "OpenAI-Sentinel-Token")
	delete(headers, "OpenAI-Sentinel-SO-Token")
	delete(headers, "OAI-Telemetry")
	return headers
}

func (w *worker) snapshotHeaders() map[string]string {
	return map[string]string{
		"User-Agent":    stripeUserAgent,
		"Accept":        "*/*",
		"Content-Type":  "application/json",
		"Authorization": "Bearer " + w.input.AccessToken,
		"Origin":        "https://chatgpt.com",
		"Referer":       fmt.Sprintf("https://chatgpt.com/checkout/%s/%s", w.input.ProcessorEntity, w.input.SessionID),
		"OAI-Language":  "en-US",
	}
}

func firstHeader(headers map[string]string, keys ...string) string {
	for _, key := range keys {
		if value := headers[key]; value != "" {
			return value
		}
	}
	return ""
}

func (w *worker) formSummary(form url.Values) map[string]any {
	out := make(map[string]any, len(form))
	for key, values := range form {
		if isSensitiveDiagnosticField(key) {
			out[key] = "[REDACTED]"
			continue
		}
		if len(values) == 1 {
			out[key] = w.scrubDiagnosticString(values[0])
		} else {
			items := make([]string, len(values))
			for index, value := range values {
				items[index] = w.scrubDiagnosticString(value)
			}
			out[key] = items
		}
	}
	return out
}

func isSensitiveDiagnosticField(key string) bool {
	normalized := strings.ToLower(key)
	for _, marker := range []string{"address", "authorization", "cookie", "email", "name", "password", "secret", "sentinel", "token"} {
		if strings.Contains(normalized, marker) {
			return true
		}
	}
	return normalized == "key"
}

func (w *worker) sanitizeDiagnosticValue(value any, field string) any {
	if value == nil {
		return nil
	}
	if isSensitiveDiagnosticField(field) {
		if text, ok := value.(string); ok && strings.TrimSpace(text) == "" {
			return ""
		}
		return "[REDACTED]"
	}
	switch item := value.(type) {
	case map[string]any:
		out := make(map[string]any, len(item))
		for key, nested := range item {
			out[key] = w.sanitizeDiagnosticValue(nested, key)
		}
		return out
	case []any:
		out := make([]any, len(item))
		for index, nested := range item {
			out[index] = w.sanitizeDiagnosticValue(nested, field)
		}
		return out
	case string:
		return w.scrubDiagnosticString(item)
	default:
		return value
	}
}

func (w *worker) scrubDiagnosticString(value string) string {
	out := value
	secrets := []string{
		w.input.AccessToken,
		w.input.CookieHeader,
		w.input.ProxyURL,
		w.input.Billing.Email,
		w.input.Billing.Name,
		w.input.ApproveHeaders["OpenAI-Sentinel-Token"],
		firstHeader(w.input.ApproveHeaders, "OpenAI-Sentinel-SO-Token", "OpenAI-Sentinel-So-Token"),
	}
	for _, secret := range secrets {
		if secret != "" {
			out = strings.ReplaceAll(out, secret, "[REDACTED]")
		}
	}
	return out
}

func stripeError(payload map[string]any) string {
	if errorObject, ok := payload["error"].(map[string]any); ok {
		for _, key := range []string{"message", "code", "type"} {
			if value := stringValue(errorObject[key]); value != "" {
				return value
			}
		}
	}
	return "upstream error"
}

func contextWithTimeout(parent context.Context, seconds int) (context.Context, context.CancelFunc) {
	return context.WithTimeout(parent, time.Duration(seconds)*time.Second)
}
