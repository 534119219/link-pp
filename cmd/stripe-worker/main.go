package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"time"

	"github.com/eatwhiteporridge/oaics-handoff/internal/stripeworker"
)

func main() {
	raw, err := io.ReadAll(io.LimitReader(os.Stdin, 2<<20))
	if err != nil {
		write(stripeworker.Output{OK: false, Code: "go_worker_read_error", Message: err.Error()})
		return
	}
	var input stripeworker.Input
	if err := json.Unmarshal(raw, &input); err != nil {
		write(stripeworker.Output{OK: false, Code: "go_worker_json_error", Message: err.Error()})
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 180*time.Second)
	defer cancel()
	write(stripeworker.Run(ctx, input))
}

func write(output stripeworker.Output) {
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(output); err != nil {
		fmt.Fprintf(os.Stderr, "encode output: %v\n", err)
		os.Exit(1)
	}
}
