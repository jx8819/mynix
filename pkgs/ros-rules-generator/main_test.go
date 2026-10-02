package main

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"log"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"
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
	clean := generateCleanList([]parsedSource{{name: "gfw1", lines: []string{"a.com", "b.com"}}}, nil, []string{"c.com"}, []string{"d.com"}, []string{"a.com"}, true)
	out = buf.String()
	if !strings.Contains(out, "[DEBUG] clean-list: 2 raw domains, -1 direct whitelist, +1 custom proxy, +1 clean-ip -> 3 unique domains") {
		t.Errorf("missing clean-list debug log: %s", out)
	}
	if len(clean) != 3 {
		t.Errorf("expected 3 unique domains, got %d", len(clean))
	}

	// 4. Domain RSC and Clash GFW debug logging
	buf.Reset()
	_ = generateDomainRsc(clean, true)
	out = buf.String()
	if !strings.Contains(out, "[DEBUG] domain.rsc: generated 3 static DNS FWD entries") {
		t.Errorf("missing domain.rsc debug log: %s", out)
	}

	buf.Reset()
	_ = generateClashGfwList(clean, true)
	out = buf.String()
	if !strings.Contains(out, "[DEBUG] clash-gfw-list: generated 3 payload items") {
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

func TestParseDoHAnswers(t *testing.T) {
	raw := []byte(`{"Status":0,"Answer":[` +
		`{"name":"a.test","type":1,"data":"99.84.215.46"},` +
		`{"name":"a.test","type":28,"data":"2600:9000::1"},` +
		`{"name":"a.test","type":5,"data":"cdn.example."},` +
		`{"name":"a.test","type":1,"data":"not-an-ip"}]}`)
	ips, err := parseDoHAnswers("a.test", raw)
	if err != nil {
		t.Fatalf("parse failed: %v", err)
	}
	if len(ips) != 1 || ips[0] != "99.84.215.46" {
		t.Fatalf("want only the A record, got %v", ips)
	}

	if _, err := parseDoHAnswers("a.test", []byte(`{"Status":3}`)); err == nil {
		t.Error("want error on non-zero rcode")
	}
	if _, err := parseDoHAnswers("a.test", []byte(`{broken`)); err == nil {
		t.Error("want error on malformed JSON")
	}
}

func TestGenerateTmdbRsc(t *testing.T) {
	out := generateTmdbRsc(map[string][]string{
		"b.test": {"2.2.2.2"},
		"a.test": {"1.1.1.1", "1.1.1.2"},
	}, false)
	want := "/ip dns static remove numbers=[/ip dns static find comment=tmdb]\n" +
		"/ip dns static\n" +
		"add name=a.test address=1.1.1.1 type=A comment=tmdb\n" +
		"add name=a.test address=1.1.1.2 type=A comment=tmdb\n" +
		"add name=b.test address=2.2.2.2 type=A comment=tmdb\n"
	if string(out) != want {
		t.Errorf("tmdb.rsc mismatch:\ngot:\n%s\nwant:\n%s", out, want)
	}
}

// newTestTLSCert issues a self-signed server certificate for the given DNS names.
func newTestTLSCert(t *testing.T, dnsNames ...string) tls.Certificate {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("gen key: %v", err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: dnsNames[0]},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		DNSNames:     dnsNames,
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("create cert: %v", err)
	}
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}
}

// startTestTLSServer serves TLS on 127.0.0.1:<random>, completing handshakes then closing.
func startTestTLSServer(t *testing.T, cert tls.Certificate) (addr string, stop func()) {
	t.Helper()
	ln, err := tls.Listen("tcp", "127.0.0.1:0", &tls.Config{Certificates: []tls.Certificate{cert}})
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				if tc, ok := c.(*tls.Conn); ok {
					_ = tc.Handshake()
				}
				_ = c.Close()
			}(c)
		}
	}()
	return ln.Addr().String(), func() {
		_ = ln.Close()
		<-done
	}
}

func TestGenerateDnsmasqForward(t *testing.T) {
	out := string(generateDnsmasqForward([]string{"b.test", "a.test"}, "10.10.10.1", 5353, false))
	want := "# generated by ros-rules-generator - do not edit\n" +
		"# forward listed domains to 10.10.10.1#5353\n" +
		"server=/b.test/10.10.10.1#5353\n" +
		"server=/a.test/10.10.10.1#5353\n"
	if out != want {
		t.Errorf("generateDnsmasqForward mismatch:\ngot:\n%q\nwant:\n%q", out, want)
	}
}

func TestNewGeneratorRejectsBadDnsmasqPort(t *testing.T) {
	if _, err := NewGenerator(t.TempDir(), "", nil, nil, nil, nil, "", "10.10.10.1", 70000, Sources{}, false); err == nil {
		t.Error("expected port 70000 to be rejected")
	}
	if _, err := NewGenerator(t.TempDir(), "", nil, nil, nil, nil, "", "", 70000, Sources{}, false); err != nil {
		t.Errorf("empty target must disable the output regardless of port: %v", err)
	}
}

func TestTelegramConverters(t *testing.T) {
	v4, v6 := parseTelegramCidrs([]byte("# comment\n91.108.4.0/22\n\n2001:b68:4001::/48\n149.154.160.0/20\n"), false)
	if len(v4) != 2 || len(v6) != 1 {
		t.Fatalf("parseTelegramCidrs got v4=%v v6=%v", v4, v6)
	}
	rsc := string(generateTelegramRsc(v4, v6, false))
	if !strings.Contains(rsc, "add list=telegram address=91.108.4.0/22 comment=telegram-official-managed") {
		t.Errorf("telegram.rsc missing managed entry: %s", rsc)
	}
	if !strings.Contains(rsc, "remove [find where list=telegram comment=telegram-official-managed]") {
		t.Errorf("telegram.rsc must remove only managed entries: %s", rsc)
	}
	mihomo := string(generateTelegramMihomo(v4, v6, false))
	if !strings.Contains(mihomo, "  - IP-CIDR,149.154.160.0/20,no-resolve") ||
		!strings.Contains(mihomo, "  - IP-CIDR6,2001:b68:4001::/48,no-resolve") {
		t.Errorf("telegram-mihomo.yaml wrong payload: %s", mihomo)
	}
}

func TestResolveTmdb(t *testing.T) {
	// 假 DoH：任何域名都答 127.0.0.1（TLS 校验端口指向本地假 TLS 服务器）
	doh := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/dns-json")
		_, _ = w.Write([]byte(`{"Status":0,"Answer":[{"name":"x","type":1,"data":"127.0.0.1"}]}`))
	}))
	defer doh.Close()

	addr, stop := startTestTLSServer(t, newTestTLSCert(t, "good.test"))
	defer stop()
	_, port, err := net.SplitHostPort(addr)
	if err != nil {
		t.Fatalf("split addr: %v", err)
	}

	oldEps, oldPort := dohEndpoints, tmdbVerifyPort
	dohEndpoints = []dohEndpoint{{url: doh.URL, accept: "application/dns-json"}}
	tmdbVerifyPort = port
	defer func() { dohEndpoints, tmdbVerifyPort = oldEps, oldPort }()

	g := &Generator{tmdbDomains: []string{"good.test"}, tmdbECS: ""}
	verified, err := g.resolveTmdb(context.Background())
	if err != nil {
		t.Fatalf("resolve good.test: %v", err)
	}
	if got := verified["good.test"]; len(got) != 1 || got[0] != "127.0.0.1" {
		t.Fatalf("want [127.0.0.1], got %v", got)
	}

	// 证书只含 good.test：bad.test 的候选 IP 全部被 SNI 校验拒掉 → 整轮必须失败
	g2 := &Generator{tmdbDomains: []string{"bad.test"}, tmdbECS: ""}
	if _, err := g2.resolveTmdb(context.Background()); err == nil {
		t.Fatal("want error when no candidate IP passes the SNI check")
	}
}
