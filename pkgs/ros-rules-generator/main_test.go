package main

import (
	"bytes"
	"context"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

func TestDebugLogging(t *testing.T) {
	var buf bytes.Buffer
	log.SetOutput(&buf)
	defer log.SetOutput(os.Stderr)

	// 1. Fetcher debug logging & URL query/fragment sanitization
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Length", "5")
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("hello"))
	}))
	defer srv.Close()

	fetcher := NewFetcher(2, true)
	data, err := fetcher.Fetch(context.Background(), srv.URL+"?token=supersecret#frag", 1024)
	if err != nil {
		t.Fatalf("fetch failed: %v", err)
	}
	if string(data) != "hello" {
		t.Fatalf("unexpected data: %q", string(data))
	}
	out := buf.String()
	if !strings.Contains(out, "[DEBUG] HTTP GET ") || !strings.Contains(out, " -> 200 (5 bytes in ") {
		t.Errorf("missing fetch debug log in: %s", out)
	}
	if strings.Contains(out, "supersecret") || strings.Contains(out, "frag") {
		t.Errorf("leak detected: secret token or fragment found in log: %s", out)
	}

	// 1b. Fetcher non-2xx debug logging
	buf.Reset()
	errSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Length", "9")
		w.WriteHeader(http.StatusNotFound)
		w.Write([]byte("not found"))
	}))
	defer errSrv.Close()

	_, err = fetcher.Fetch(context.Background(), errSrv.URL, 1024)
	if err == nil {
		t.Fatalf("expected error on 404, got nil")
	}
	out = buf.String()
	if !strings.Contains(out, "[DEBUG] HTTP GET ") || !strings.Contains(out, " -> 404 (9 bytes in ") {
		t.Errorf("missing non-2xx response debug log in: %s", out)
	}

	// 2. Parser debug logging
	buf.Reset()
	// base64 for: !comment\napple.com\nexample.com\n
	gfwRaw := []byte("IWNvbW1lbnRPbgphcHBsZS5jb20KZXhhbXBsZS5jb20K")
	res, err := parseGfwList("test-gfw", gfwRaw, true)
	if err != nil {
		t.Fatalf("parse failed: %v", err)
	}
	if len(res) != 1 || res[0] != "example.com" {
		t.Fatalf("unexpected parsed result: %v", res)
	}
	out = buf.String()
	if !strings.Contains(out, "[DEBUG] parser test-gfw: parsed 1 valid domains from ") {
		t.Errorf("missing parser debug log: %s", out)
	}

	// 3. Clean-list debug logging
	buf.Reset()
	clean := generateCleanList([]parsedSource{{name: "gfw1", lines: []string{"a.com", "b.com"}}}, nil, []string{"c.com"}, []string{"a.com"}, true)
	out = buf.String()
	if !strings.Contains(out, "[DEBUG] clean-list: 2 raw domains, -1 direct whitelist, +1 custom proxy -> 2 unique domains") {
		t.Errorf("missing clean-list debug log: %s", out)
	}
	if len(clean) != 2 {
		t.Errorf("expected 2 unique domains, got %d", len(clean))
	}

	// 4. Domain RSC and Clash GFW debug logging
	buf.Reset()
	_ = generateDomainRsc(clean, true)
	out = buf.String()
	if !strings.Contains(out, "[DEBUG] domain.rsc: generated 2 static DNS FWD entries") {
		t.Errorf("missing domain.rsc debug log: %s", out)
	}

	buf.Reset()
	_ = generateClashGfwList(clean, true)
	out = buf.String()
	if !strings.Contains(out, "[DEBUG] clash-gfw-list: generated 2 payload items") {
		t.Errorf("missing clash-gfw-list debug log: %s", out)
	}

	// 5. filterYamlNonAscii debug logging & splitRawLines line count
	buf.Reset()
	yamlInput := []byte("payload:\n  - 'ascii.com'\n  - '非ascii.com'\n")
	filtered := filterYamlNonAscii("test.yaml", yamlInput, true)
	out = buf.String()
	if !strings.Contains(out, "[DEBUG] test.yaml: stripped 1 non-ASCII payload lines") {
		t.Errorf("missing filterYamlNonAscii debug log: %s", out)
	}
	if strings.Contains(string(filtered), "非ascii.com") {
		t.Errorf("non-ascii line was not stripped")
	}
	if strings.HasSuffix(string(filtered), "\n\n") {
		t.Errorf("extra trailing newline introduced by filtering")
	}

	// 6. Test splitRawLines trailing newline behavior
	rawWithNewline := []byte("line1\nline2\n")
	lines := splitRawLines(rawWithNewline)
	if len(lines) != 2 {
		t.Errorf("expected 2 lines, got %d: %v", len(lines), lines)
	}
}
