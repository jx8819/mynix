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
	"fmt"
	"io"
	"log"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
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
	if out == "" {
		t.Error("debug-enabled fetch produced no diagnostic output")
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
	if out == "" {
		t.Error("debug-enabled failed fetch produced no diagnostic output")
	}

	// 2. Parser behavior with debug enabled
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

	// 3. Clean-list behavior with debug enabled
	buf.Reset()
	clean := generateCleanList([]parsedSource{{name: "gfw1", lines: []string{"a.com", "b.com"}}}, nil, []string{"c.com"}, []string{"d.com"}, []string{"a.com"}, true)
	if len(clean) != 3 {
		t.Errorf("expected 3 unique domains, got %d", len(clean))
	}

	// 4. Domain RSC and Clash GFW behavior with debug enabled
	buf.Reset()
	state := make(map[string]domainRscEntry)
	applyDomainRsc(t, state, generateDomainRsc(clean, nil, true))
	gfw := domainRscEntry{kind: "FWD", comment: "gfw", forwardTo: "clash", matchSubdomain: "yes", addressList: "gfwlist"}
	assertDomainRscState(t, state, map[string]domainRscEntry{
		"b.com": gfw, "c.com": gfw, "d.com": gfw,
	})

	buf.Reset()
	clash := generateClashGfwList(clean, true)
	if string(clash) != "payload:\n  - '+.b.com'\n  - '+.c.com'\n  - '+.d.com'\n" {
		t.Errorf("unexpected Clash GFW payload: %s", clash)
	}

	// 5. filterYamlNonAscii behavior with debug enabled
	buf.Reset()
	yamlInput := []byte("payload:\n  - 'ascii.com'\n  - '非ascii.com'\n")
	filtered := filterYamlNonAscii("test.yaml", yamlInput, true)
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
	out := string(generateDnsmasqForward([]string{"b.test", "a.test"}, nil, "10.10.10.1", 5353, false))
	want := "# generated by ros-rules-generator - do not edit\n" +
		"# forward listed domains to 10.10.10.1#5353\n" +
		"server=/b.test/10.10.10.1#5353\n" +
		"server=/a.test/10.10.10.1#5353\n"
	if out != want {
		t.Errorf("generateDnsmasqForward mismatch:\ngot:\n%q\nwant:\n%q", out, want)
	}
	// tvDomains 非空时追加同一文件，格式与 gfw 行完全一致
	out = string(generateDnsmasqForward([]string{"b.test", "a.test"}, []string{"tv.test"}, "10.10.10.1", 5353, false))
	want = "# generated by ros-rules-generator - do not edit\n" +
		"# forward listed domains to 10.10.10.1#5353\n" +
		"server=/b.test/10.10.10.1#5353\n" +
		"server=/a.test/10.10.10.1#5353\n" +
		"server=/tv.test/10.10.10.1#5353\n"
	if out != want {
		t.Errorf("generateDnsmasqForward tv mismatch:\ngot:\n%q\nwant:\n%q", out, want)
	}
}

func TestNewGeneratorRejectsUnsafeDnsmasqForwards(t *testing.T) {
	tests := []struct {
		name     string
		forwards []DnsmasqForward
		want     string
	}{
		{
			name:     "path separator",
			forwards: []DnsmasqForward{{Name: "../escape", Target: "127.0.0.1", Port: 53}},
			want:     "name",
		},
		{
			name: "duplicate name",
			forwards: []DnsmasqForward{
				{Name: "default", Target: "127.0.0.1", Port: 53},
				{Name: "default", Target: "192.0.2.1", Port: 53},
			},
			want: "duplicate",
		},
		{
			name:     "injected target",
			forwards: []DnsmasqForward{{Name: "default", Target: "127.0.0.1\nserver=/bad/evil", Port: 53}},
			want:     "target",
		},
		{
			name:     "zero port",
			forwards: []DnsmasqForward{{Name: "default", Target: "127.0.0.1", Port: 0}},
			want:     "port",
		},
		{
			name:     "oversized port",
			forwards: []DnsmasqForward{{Name: "default", Target: "127.0.0.1", Port: 65536}},
			want:     "port",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, err := NewGenerator(filepath.Join(t.TempDir(), "lists"), "", nil, nil, nil, nil, nil, "", tt.forwards, Sources{}, false)
			if err == nil || !strings.Contains(err.Error(), tt.want) {
				t.Fatalf("NewGenerator(%v): got error %v, want %q", tt.forwards, err, tt.want)
			}
		})
	}
}

type dnsmasqTestTransport struct{}

func (dnsmasqTestTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	body := ""
	switch req.URL.String() {
	case telegramCidrURL:
		body = "91.108.4.0/22\n"
	case "https://example.test/google", "https://example.test/rules":
		body = "DOMAIN-SUFFIX,example.test\n"
	default:
		return nil, fmt.Errorf("unexpected request %s", req.URL)
	}
	return &http.Response{
		StatusCode:    http.StatusOK,
		Header:        make(http.Header),
		Body:          io.NopCloser(strings.NewReader(body)),
		ContentLength: int64(len(body)),
		Request:       req,
	}, nil
}

func TestRunDnsmasqVariants(t *testing.T) {
	dir := t.TempDir()
	outDir := filepath.Join(dir, "lists")
	forwardsFile := filepath.Join(dir, "forwards.json")
	config := `[
		{"name":"default","target":"127.0.0.1","port":1053},
		{"name":"west","target":"192.0.2.53","port":53},
		{"name":"east","target":"dns.example","port":853}
	]`
	if err := os.WriteFile(forwardsFile, []byte(config), 0600); err != nil {
		t.Fatal(err)
	}
	forwards, err := loadDnsmasqForwards(forwardsFile)
	if err != nil {
		t.Fatal(err)
	}
	newGenerator := func(forwards []DnsmasqForward) *Generator {
		t.Helper()
		gen, err := NewGenerator(outDir, "", nil, []string{"b.test", "a.test"}, nil, nil, nil, "", forwards,
			Sources{Google: []Source{{Name: "google", URL: "https://example.test/google"}}}, false)
		if err != nil {
			t.Fatal(err)
		}
		gen.fetcher.client.Transport = dnsmasqTestTransport{}
		return gen
	}
	if err := newGenerator(forwards).Run(context.Background()); err != nil {
		t.Fatal(err)
	}

	expected := map[string]string{
		"clean-list.txt":           "a.test\nb.test\n",
		"domain.dnsmasq.conf":      "# generated by ros-rules-generator - do not edit\n# forward listed domains to 127.0.0.1#1053\nserver=/a.test/127.0.0.1#1053\nserver=/b.test/127.0.0.1#1053\n",
		"domain.dnsmasq.west.conf": "# generated by ros-rules-generator - do not edit\n# forward listed domains to 192.0.2.53#53\nserver=/a.test/192.0.2.53#53\nserver=/b.test/192.0.2.53#53\n",
		"domain.dnsmasq.east.conf": "# generated by ros-rules-generator - do not edit\n# forward listed domains to dns.example#853\nserver=/a.test/dns.example#853\nserver=/b.test/dns.example#853\n",
	}
	for path, want := range expected {
		got, err := os.ReadFile(filepath.Join(outDir, path))
		if err != nil {
			t.Fatal(err)
		}
		if string(got) != want {
			t.Errorf("%s: got %q, want %q", path, got, want)
		}
	}
	if err := os.WriteFile(forwardsFile, []byte("[]"), 0600); err != nil {
		t.Fatal(err)
	}
	none, err := loadDnsmasqForwards(forwardsFile)
	if err != nil {
		t.Fatal(err)
	}
	if err := newGenerator(none).Run(context.Background()); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"domain.dnsmasq.conf", "domain.dnsmasq.west.conf", "domain.dnsmasq.east.conf"} {
		if _, err := os.Stat(filepath.Join(outDir, name)); !os.IsNotExist(err) {
			t.Errorf("disabled dnsmasq variant %s remains after atomic swap: %v", name, err)
		}
	}
	if _, err := os.Stat(filepath.Join(outDir, "domain.rsc")); err != nil {
		t.Errorf("base output disappeared: %v", err)
	}
}

func TestRunRejectsDnsmasqOutputCollision(t *testing.T) {
	for _, output := range []string{"domain.dnsmasq.west.conf", "domain.dnsmasq.west.conf/child"} {
		t.Run(output, func(t *testing.T) {
			dir := t.TempDir()
			outDir := filepath.Join(dir, "lists")
			gen, err := NewGenerator(outDir, "", nil, []string{"a.test"}, nil, nil, nil, "",
				[]DnsmasqForward{{Name: "west", Target: "127.0.0.1", Port: 53}},
				Sources{
					Google: []Source{{Name: "google", URL: "https://example.test/google"}},
					Rules:  []RuleSource{{Name: "colliding", URL: "https://example.test/rules", Output: output}},
				}, false)
			if err != nil {
				t.Fatal(err)
			}
			gen.fetcher.client.Transport = dnsmasqTestTransport{}
			if err := gen.Run(context.Background()); err == nil || !strings.Contains(err.Error(), "output path") {
				t.Fatalf("expected conflicting output path to fail, got %v", err)
			}
			if _, err := os.Lstat(outDir); !os.IsNotExist(err) {
				t.Errorf("invalid run should not deploy files: %v", err)
			}
			entries, err := os.ReadDir(dir)
			if err != nil {
				t.Fatal(err)
			}
			for _, entry := range entries {
				if strings.HasPrefix(entry.Name(), ".lists-tmp-") {
					t.Errorf("invalid output created staging directory %s", entry.Name())
				}
			}
		})
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

// TestCheckOutputsSetValidation 覆盖产物清单集合校验：期望文件名集合 vs 实际产出集合。
func TestCheckOutputsSetValidation(t *testing.T) {
	g := &Generator{
		sources:         Sources{Rules: []RuleSource{{Name: "r", URL: "https://example.test/rules", Output: "rules/r.txt"}}},
		dnsmasqForwards: []DnsmasqForward{{Name: "west", Target: "127.0.0.1", Port: 53}},
	}
	fullSet := func() []fileEntry {
		files := make([]fileEntry, 0, len(baseOutputPaths)+2)
		for _, p := range baseOutputPaths {
			files = append(files, fileEntry{path: p, content: []byte("x")})
		}
		files = append(files,
			fileEntry{path: "domain.dnsmasq.west.conf", content: []byte("x")},
			fileEntry{path: "rules/r.txt", content: []byte("x")})
		return files
	}

	if err := g.checkOutputs(fullSet(), false, false); err != nil {
		t.Fatalf("complete output set rejected: %v", err)
	}

	missing := fullSet()
	missing[0].path = "clean-list.missing"
	if err := g.checkOutputs(missing, false, false); err == nil || !strings.Contains(err.Error(), "internal inconsistency") {
		t.Fatalf("mismatched output set accepted: %v", err)
	}

	extra := append(fullSet(), fileEntry{path: "unexpected.txt", content: []byte("x")})
	if err := g.checkOutputs(extra, false, false); err == nil || !strings.Contains(err.Error(), "internal inconsistency") {
		t.Fatalf("unexpected extra output accepted: %v", err)
	}

	// TMDB 期望集合与开关对齐：关闭时多出 tmdb.rsc、开启时缺 tmdb.rsc 都必须拒绝
	if err := g.checkOutputs(append(fullSet(), fileEntry{path: "tmdb.rsc", content: []byte("x")}), false, false); err == nil {
		t.Fatal("tmdb.rsc accepted while tmdb disabled")
	}
	if err := g.checkOutputs(fullSet(), true, false); err == nil {
		t.Fatal("missing tmdb.rsc accepted while tmdb enabled")
	}

	// Both optional TV artifacts are required together, and forbidden when disabled.
	tvPaths := []string{"tv.rsc", "rules/tv.yaml"}
	tvSet := fullSet()
	for _, path := range tvPaths {
		artifact := fileEntry{path: path, content: []byte("x")}
		tvSet = append(tvSet, artifact)
		if err := g.checkOutputs(append(fullSet(), artifact), false, false); err == nil {
			t.Fatalf("%s accepted while tv disabled", path)
		}
	}
	if err := g.checkOutputs(tvSet, false, true); err != nil {
		t.Fatalf("complete TV artifact set rejected: %v", err)
	}
	if err := g.checkOutputs(fullSet(), false, true); err == nil {
		t.Fatal("missing TV artifacts accepted while tv enabled")
	}
	for _, missingPath := range tvPaths {
		files := fullSet()
		for _, path := range tvPaths {
			if path != missingPath {
				files = append(files, fileEntry{path: path, content: []byte("x")})
			}
		}
		if err := g.checkOutputs(files, false, true); err == nil {
			t.Fatalf("missing %s accepted while tv enabled", missingPath)
		}
	}

	// 重复路径不改集合判定（与旧计数校验一致），仍由重复路径检查拒绝
	dup := append(fullSet(), fileEntry{path: "rules/r.txt", content: []byte("y")})
	if err := g.checkOutputs(dup, false, false); err == nil || !strings.Contains(err.Error(), "duplicate output path") {
		t.Fatalf("duplicate output path not rejected: %v", err)
	}
}

type domainRscEntry struct {
	kind, comment, forwardTo, matchSubdomain, addressList string
}

// parseDomainRscFields decodes the generated RouterOS words, including quoted
// selectors and escaped names. An unescaped $ would expand on the real router.
func parseDomainRscFields(t *testing.T, text string) map[string]string {
	t.Helper()
	fields := make(map[string]string)
	var word strings.Builder
	quoted, escaped := false, false
	for _, ch := range text + " " {
		switch {
		case escaped:
			switch ch {
			case 'n':
				ch = '\n'
			case 'r':
				ch = '\r'
			case 't':
				ch = '\t'
			}
			word.WriteRune(ch)
			escaped = false
		case ch == '\\' && quoted:
			escaped = true
		case ch == '"':
			quoted = !quoted
		case ch == '$' && quoted:
			t.Fatal("unescaped RouterOS variable expansion in DNS value")
		case ch == ' ' && !quoted:
			if word.Len() > 0 {
				key, value, ok := strings.Cut(word.String(), "=")
				if !ok {
					t.Fatalf("unsupported DNS field: %s", word.String())
				}
				fields[key] = value
				word.Reset()
			}
		default:
			word.WriteRune(ch)
		}
	}
	if quoted || escaped {
		t.Fatalf("unterminated RouterOS DNS value: %s", text)
	}
	return fields
}

// applyDomainRsc models the generated DNS commands, including RouterOS rejecting
// duplicate names and imports retaining entries created before an error.
func applyDomainRsc(t *testing.T, state map[string]domainRscEntry, script []byte) {
	t.Helper()
	cleared := make(map[string]bool)
	added := false
	fullRefresh := bytes.Contains(script, []byte("/ip dns static remove numbers=["))
	for _, line := range strings.Split(string(script), "\n") {
		line = strings.TrimSpace(line)
		switch {
		case line == "", line == "/ip dns static":
			continue
		case strings.HasPrefix(line, "/ip dns static remove "):
			if added {
				t.Fatal("managed DNS cleanup must precede every add")
			}
			_, selector, ok := strings.Cut(line, "find comment=")
			if !ok || !strings.HasSuffix(selector, "]") {
				t.Fatalf("unsupported DNS removal: %s", line)
			}
			comment := strings.TrimSuffix(selector, "]")
			if comment != "gfw" && comment != "tv" {
				t.Fatalf("removal targets unmanaged DNS entries: %s", line)
			}
			for name, entry := range state {
				if entry.comment == comment {
					delete(state, name)
				}
			}
			cleared[comment] = true
		case strings.HasPrefix(line, ":foreach entry in=[/ip dns static find where "):
			const prefix = ":foreach entry in=[/ip dns static find where "
			const suffix = "] do={ /ip dns static remove $entry }"
			if !strings.HasSuffix(line, suffix) {
				t.Fatalf("unsupported DNS foreach removal: %s", line)
			}
			selector := parseDomainRscFields(t, strings.TrimSuffix(strings.TrimPrefix(line, prefix), suffix))
			if len(selector) != 1 {
				t.Fatalf("unsupported DNS selector: %s", line)
			}
			if comment, ok := selector["comment"]; ok {
				if comment != "tv" || added {
					t.Fatalf("TV-only cleanup must target TV before additions: %s", line)
				}
				for name, entry := range state {
					if entry.comment == comment {
						delete(state, name)
					}
				}
				cleared[comment] = true
			} else if name, ok := selector["name"]; ok && cleared["tv"] && !fullRefresh {
				delete(state, name)
			} else {
				t.Fatalf("unsupported exact-name upsert: %s", line)
			}
		case strings.HasPrefix(line, "add "), strings.HasPrefix(line, "/ip dns static add "):
			if !cleared["tv"] || (fullRefresh && !cleared["gfw"]) {
				t.Fatal("managed DNS cleanup must precede additions")
			}
			added = true
			fields := parseDomainRscFields(t, strings.TrimPrefix(strings.TrimPrefix(line, "/ip dns static "), "add "))
			name := fields["name"]
			if name == "" {
				t.Fatal("DNS add is missing a name")
			}
			if _, exists := state[name]; exists {
				t.Fatalf("RouterOS import would abort on duplicate DNS name %q", name)
			}
			state[name] = domainRscEntry{
				kind: fields["type"], comment: fields["comment"], forwardTo: fields["forward-to"],
				matchSubdomain: fields["match-subdomain"], addressList: fields["address-list"],
			}
		default:
			t.Fatalf("unsupported DNS command: %s", line)
		}
	}
	if !cleared["tv"] || (fullRefresh && !cleared["gfw"]) {
		t.Fatal("refresh did not clear its managed DNS groups")
	}
}

func assertDomainRscState(t *testing.T, got, want map[string]domainRscEntry) {
	t.Helper()
	if len(got) != len(want) {
		t.Errorf("DNS entry count: got %d, want %d; entries: %v", len(got), len(want), got)
	}
	for name, entry := range want {
		if actual, ok := got[name]; !ok || actual != entry {
			t.Errorf("DNS entry %q: got %+v (exists=%t), want %+v", name, actual, ok, entry)
		}
	}
}

func TestGenerateDomainRscTvOverlap(t *testing.T) {
	state := make(map[string]domainRscEntry)
	applyDomainRsc(t, state, generateDomainRsc(
		[]string{"a.test", "bbc.co.uk", "bbci.co.uk", "bbc.com"},
		[]string{"bbc.co.uk", "bbci.co.uk", "bbc.com", "bbci.co.uk", "bbc-cdn.test"}, true))
	gfw := domainRscEntry{kind: "FWD", comment: "gfw", forwardTo: "clash", matchSubdomain: "yes", addressList: "gfwlist"}
	tv := domainRscEntry{kind: "FWD", comment: "tv", forwardTo: "clash", matchSubdomain: "yes"}
	assertDomainRscState(t, state, map[string]domainRscEntry{
		"a.test": gfw, "bbc.co.uk": tv, "bbci.co.uk": tv, "bbc.com": tv, "bbc-cdn.test": tv,
	})
}

func TestGenerateDomainRscRefresh(t *testing.T) {
	gfw := domainRscEntry{kind: "FWD", comment: "gfw", forwardTo: "clash", matchSubdomain: "yes", addressList: "gfwlist"}
	tv := domainRscEntry{kind: "FWD", comment: "tv", forwardTo: "clash", matchSubdomain: "yes"}
	manual := domainRscEntry{kind: "FWD", comment: "manual", forwardTo: "192.0.2.53"}
	state := map[string]domainRscEntry{
		"stale-gfw.test": gfw, "stale-tv.test": tv, "manual.test": manual,
	}
	for _, step := range []struct {
		name        string
		domains, tv []string
	}{
		{name: "initial", domains: []string{"a.test"}, tv: []string{"b.test"}},
		{name: "swap groups", domains: []string{"b.test"}, tv: []string{"a.test"}},
		{name: "repeat import", domains: []string{"b.test"}, tv: []string{"a.test"}},
		{name: "disable nil TV", domains: []string{"a.test", "b.test"}},
		{name: "reenable TV", domains: []string{"a.test"}, tv: []string{"b.test"}},
		{name: "disable empty TV", domains: []string{"b.test"}, tv: []string{}},
		{name: "clear all managed entries"},
	} {
		t.Run(step.name, func(t *testing.T) {
			applyDomainRsc(t, state, generateDomainRsc(step.domains, step.tv, false))
			want := map[string]domainRscEntry{"manual.test": manual}
			for _, name := range step.domains {
				want[name] = gfw
			}
			for _, name := range step.tv {
				want[name] = tv
			}
			assertDomainRscState(t, state, want)
		})
	}
}

func TestDomainAndTvRscImportOrders(t *testing.T) {
	gfw := domainRscEntry{kind: "FWD", comment: "gfw", forwardTo: "clash", matchSubdomain: "yes", addressList: "gfwlist"}
	tv := domainRscEntry{kind: "FWD", comment: "tv", forwardTo: "clash", matchSubdomain: "yes"}
	manual := domainRscEntry{kind: "FWD", comment: "manual", forwardTo: "192.0.2.53"}
	runtimeFile := filepath.Join(t.TempDir(), "tv-domains.txt")
	render := func(runtimeDomains string) map[string][]byte {
		t.Helper()
		if err := os.WriteFile(runtimeFile, []byte(runtimeDomains), 0644); err != nil {
			t.Fatal(err)
		}
		merged, err := loadDomainList("anchor.test", runtimeFile, false, "tv")
		if err != nil {
			t.Fatal(err)
		}
		g := &Generator{tvDomains: merged}
		files, err := g.assembleFiles(context.Background(), nil, []string{"gfw.test", "new-tv.test", "overlap.test"}, nil)
		if err != nil {
			t.Fatal(err)
		}
		feeds := make(map[string][]byte)
		for _, file := range files {
			if file.path == "domain.rsc" || file.path == "tv.rsc" {
				feeds[file.path] = file.content
			}
		}
		if len(feeds) != 2 {
			t.Fatalf("expected both DNS feeds from merged TV input, got %v", feeds)
		}
		return feeds
	}
	generations := []struct {
		name  string
		feeds map[string][]byte
		want  map[string]domainRscEntry
	}{
		{
			name:  "initial",
			feeds: render("overlap.test\nold-tv.test\nanchor.test\noverlap.test\n"),
			want: map[string]domainRscEntry{
				"manual.test": manual, "gfw.test": gfw, "new-tv.test": gfw,
				"anchor.test": tv, "overlap.test": tv, "old-tv.test": tv,
			},
		},
		{
			name:  "changed TV catalog",
			feeds: render("overlap.test\nnew-tv.test\n"),
			want: map[string]domainRscEntry{
				"manual.test": manual, "gfw.test": gfw,
				"anchor.test": tv, "overlap.test": tv, "new-tv.test": tv,
			},
		},
	}
	for _, test := range []struct {
		name  string
		order []string
	}{
		{name: "full-TV-full", order: []string{"domain.rsc", "tv.rsc", "domain.rsc"}},
		{name: "TV-full-TV", order: []string{"tv.rsc", "domain.rsc", "tv.rsc"}},
	} {
		t.Run(test.name, func(t *testing.T) {
			state := map[string]domainRscEntry{
				"manual.test": manual, "gfw.test": gfw, "overlap.test": gfw, "new-tv.test": gfw,
			}
			for _, generation := range generations {
				t.Run(generation.name, func(t *testing.T) {
					for range 2 {
						for _, product := range test.order {
							applyDomainRsc(t, state, generation.feeds[product])
							assertDomainRscState(t, state, generation.want)
						}
					}
				})
			}
		})
	}
}

func TestGenerateTvRscRefresh(t *testing.T) {
	gfw := domainRscEntry{kind: "FWD", comment: "gfw", forwardTo: "clash", matchSubdomain: "yes", addressList: "gfwlist"}
	tv := domainRscEntry{kind: "FWD", comment: "tv", forwardTo: "clash", matchSubdomain: "yes"}
	manual := domainRscEntry{kind: "FWD", comment: "manual", forwardTo: "192.0.2.53"}
	// No old TV group: first import must still work, taking over only exact TV
	// names even when their previous type or GFW forwarding settings differ.
	state := map[string]domainRscEntry{
		"overlap.test":  {kind: "FWD", comment: "gfw", forwardTo: "old-upstream", addressList: "gfwlist"},
		"takeover.test": {kind: "A", comment: "manual"},
		"gfw.test":      gfw,
		"manual.test":   manual,
	}
	applyDomainRsc(t, state, generateTvRsc([]string{"overlap.test", "takeover.test", "old-tv.test"}, false))
	assertDomainRscState(t, state, map[string]domainRscEntry{
		"gfw.test": gfw, "manual.test": manual,
		"overlap.test": tv, "takeover.test": tv, "old-tv.test": tv,
	})

	// The next catalog changes the TV set and encounters another old GFW name.
	state["new-tv.test"] = gfw
	state["overlap.test"] = domainRscEntry{kind: "FWD", comment: "tv", forwardTo: "wrong", addressList: "stale"}
	refresh := generateTvRsc([]string{"overlap.test", "new-tv.test"}, false)
	for range 2 {
		applyDomainRsc(t, state, refresh)
		assertDomainRscState(t, state, map[string]domainRscEntry{
			"gfw.test": gfw, "manual.test": manual, "overlap.test": tv, "new-tv.test": tv,
		})
	}
}

func TestGenerateTvRscQuotedNamesAndLargeCatalog(t *testing.T) {
	// Exercise RouterOS quoting independently of domain-list input validation.
	domains := []string{"odd\"$name\\segment\r\n\t.test"}
	want := make(map[string]domainRscEntry)
	tv := domainRscEntry{kind: "FWD", comment: "tv", forwardTo: "clash", matchSubdomain: "yes"}
	for i := range 600 {
		domains = append(domains, fmt.Sprintf("channel-%03d.test", i))
	}
	for _, domain := range domains {
		want[domain] = tv
	}
	state := make(map[string]domainRscEntry)
	script := generateTvRsc(domains, false)
	applyDomainRsc(t, state, script)
	assertDomainRscState(t, state, want)
	applyDomainRsc(t, state, script)
	assertDomainRscState(t, state, want)
}

func TestLoadTvDomainsRuntimeFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "tv-domains.txt")
	if err := os.WriteFile(path, []byte("discovered.test\nanchor.test\n"), 0644); err != nil {
		t.Fatal(err)
	}
	domains, err := loadDomainList("anchor.test,static.test", path, false, "tv")
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(sortUniqStrings(domains), ","); got != "anchor.test,discovered.test,static.test" {
		t.Fatalf("static/runtime TV merge: %s", got)
	}
	// A valid static anchor must not hide a missing runtime catalog file.
	domains, err = loadDomainList("anchor.test", path+".missing", false, "tv")
	if err == nil || domains != nil {
		t.Fatalf("missing TV runtime file did not fail closed: domains=%v, err=%v", domains, err)
	}
}

// TestGenerateTvYaml 覆盖 rules/tv.yaml 的 classical rule-provider payload 形态。
func TestGenerateTvYaml(t *testing.T) {
	got := string(generateTvYaml([]string{"tv-a.test", "tv-b.test"}, false))
	want := "payload:\n" +
		"  - DOMAIN-SUFFIX,tv-a.test\n" +
		"  - DOMAIN-SUFFIX,tv-b.test\n"
	if got != want {
		t.Errorf("rules/tv.yaml mismatch:\ngot:\n%q\nwant:\n%q", got, want)
	}
}

// TestRunTvDomains covers both TV artifacts, domain.rsc, every dnsmasq variant,
// and exclusion from the GFW lists. Disabling TV removes both optional artifacts
// from the next complete generation and clears the old TV DNS group.
func TestRunTvDomains(t *testing.T) {
	dir := t.TempDir()
	outDir := filepath.Join(dir, "lists")
	forwards := []DnsmasqForward{
		{Name: "default", Target: "127.0.0.1", Port: 1053},
		{Name: "west", Target: "192.0.2.53", Port: 53},
	}
	newGenerator := func(tvDomains []string) *Generator {
		t.Helper()
		gen, err := NewGenerator(outDir, "", nil, []string{"b.test", "a.test"}, nil, nil, tvDomains, "", forwards,
			Sources{Google: []Source{{Name: "google", URL: "https://example.test/google"}}}, false)
		if err != nil {
			t.Fatal(err)
		}
		gen.fetcher.client.Transport = dnsmasqTestTransport{}
		return gen
	}

	if err := newGenerator([]string{"tv-b.test", "tv-a.test", "tv-a.test"}).Run(context.Background()); err != nil {
		t.Fatal(err)
	}
	domainRsc, err := os.ReadFile(filepath.Join(outDir, "domain.rsc"))
	if err != nil {
		t.Fatal(err)
	}
	state := make(map[string]domainRscEntry)
	applyDomainRsc(t, state, domainRsc)
	gfw := domainRscEntry{kind: "FWD", comment: "gfw", forwardTo: "clash", matchSubdomain: "yes", addressList: "gfwlist"}
	tv := domainRscEntry{kind: "FWD", comment: "tv", forwardTo: "clash", matchSubdomain: "yes"}
	assertDomainRscState(t, state, map[string]domainRscEntry{
		"a.test": gfw, "b.test": gfw, "tv-a.test": tv, "tv-b.test": tv,
	})
	tvRsc, err := os.ReadFile(filepath.Join(outDir, "tv.rsc"))
	if err != nil {
		t.Fatal(err)
	}
	applyDomainRsc(t, state, tvRsc)
	assertDomainRscState(t, state, map[string]domainRscEntry{
		"a.test": gfw, "b.test": gfw, "tv-a.test": tv, "tv-b.test": tv,
	})
	expected := map[string]string{
		"rules/tv.yaml": "payload:\n" +
			"  - DOMAIN-SUFFIX,tv-a.test\n" +
			"  - DOMAIN-SUFFIX,tv-b.test\n",
		"domain.dnsmasq.conf": "# generated by ros-rules-generator - do not edit\n" +
			"# forward listed domains to 127.0.0.1#1053\n" +
			"server=/a.test/127.0.0.1#1053\n" +
			"server=/b.test/127.0.0.1#1053\n" +
			"server=/tv-a.test/127.0.0.1#1053\n" +
			"server=/tv-b.test/127.0.0.1#1053\n",
		"domain.dnsmasq.west.conf": "# generated by ros-rules-generator - do not edit\n" +
			"# forward listed domains to 192.0.2.53#53\n" +
			"server=/a.test/192.0.2.53#53\n" +
			"server=/b.test/192.0.2.53#53\n" +
			"server=/tv-a.test/192.0.2.53#53\n" +
			"server=/tv-b.test/192.0.2.53#53\n",
		// tv 域名只走 DNS 转发，不进 mylist / clash-gfw-list 集合
		"clean-list.txt":     "a.test\nb.test\n",
		"clash-gfw-list.txt": "payload:\n  - '+.a.test'\n  - '+.b.test'\n",
	}
	for path, want := range expected {
		got, err := os.ReadFile(filepath.Join(outDir, path))
		if err != nil {
			t.Fatal(err)
		}
		if string(got) != want {
			t.Errorf("%s: got %q, want %q", path, got, want)
		}
	}

	// Empty TV input removes both optional artifacts in the next atomic generation.
	if err := newGenerator(nil).Run(context.Background()); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{"tv.rsc", "rules/tv.yaml"} {
		if _, err := os.Stat(filepath.Join(outDir, path)); !os.IsNotExist(err) {
			t.Errorf("%s emitted with empty tvDomains: %v", path, err)
		}
	}
	domainRsc, err = os.ReadFile(filepath.Join(outDir, "domain.rsc"))
	if err != nil {
		t.Fatal(err)
	}
	applyDomainRsc(t, state, domainRsc)
	assertDomainRscState(t, state, map[string]domainRscEntry{"a.test": gfw, "b.test": gfw})
	for path, want := range map[string]string{
		"domain.dnsmasq.conf": "# generated by ros-rules-generator - do not edit\n" +
			"# forward listed domains to 127.0.0.1#1053\n" +
			"server=/a.test/127.0.0.1#1053\n" +
			"server=/b.test/127.0.0.1#1053\n",
	} {
		got, err := os.ReadFile(filepath.Join(outDir, path))
		if err != nil {
			t.Fatal(err)
		}
		if string(got) != want {
			t.Errorf("empty tvDomains %s changed: got %q, want %q", path, got, want)
		}
	}
}
