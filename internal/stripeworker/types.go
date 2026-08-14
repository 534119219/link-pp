package stripeworker

import "encoding/json"

const (
	stripeAPI           = "https://api.stripe.com"
	chatGPTBase         = "https://chatgpt.com"
	stripeVersionBase   = "2025-03-31.basil"
	stripeVersionFull   = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
	payPalStripeVersion = "2020-08-27;custom_checkout_beta=v1; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
	stripeRuntime       = "6f8494a281"
	stripeUserAgent     = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0"
	maxResponseBytes    = 2 << 20
	defaultHTTPTimeout  = 35
)

type Address struct {
	Country    string `json:"country"`
	Line1      string `json:"line1"`
	City       string `json:"city"`
	PostalCode string `json:"postal_code"`
	State      string `json:"state"`
}

type Billing struct {
	Name    string  `json:"name"`
	Email   string  `json:"email"`
	Address Address `json:"address"`
}

type Input struct {
	SessionID       string            `json:"session_id"`
	PublishableKey  string            `json:"publishable_key"`
	ProxyURL        string            `json:"proxy_url"`
	AccessToken     string            `json:"access_token"`
	CookieHeader    string            `json:"cookie_header"`
	DeviceID        string            `json:"device_id"`
	Country         string            `json:"country"`
	BrowserLocale   string            `json:"browser_locale"`
	BrowserTimezone string            `json:"browser_timezone"`
	ProcessorEntity string            `json:"processor_entity"`
	CheckoutURL     string            `json:"checkout_url"`
	Billing         Billing           `json:"billing"`
	ApproveHeaders  map[string]string `json:"approve_headers"`
	ApplyPromo      bool              `json:"apply_promo"`
	StripeBase      string            `json:"stripe_base,omitempty"`
	ChatGPTBase     string            `json:"chatgpt_base,omitempty"`
}

type Diagnostic struct {
	Kind       string          `json:"kind"`
	Method     string          `json:"method"`
	Route      string          `json:"route"`
	HTTPStatus int             `json:"http_status"`
	Request    any             `json:"request,omitempty"`
	Response   json.RawMessage `json:"response,omitempty"`
	Error      string          `json:"error,omitempty"`
}

type Output struct {
	OK          bool         `json:"ok"`
	Code        string       `json:"code"`
	Message     string       `json:"message,omitempty"`
	RedirectURL string       `json:"redirect_url,omitempty"`
	Diagnostics []Diagnostic `json:"diagnostics,omitempty"`
}

type workerError struct {
	code    string
	message string
}

func (e *workerError) Error() string { return e.message }
