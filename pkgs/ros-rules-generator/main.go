package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	defaultUserAgent = "ros-rules-generator/1.0"
	defaultTimeout   = 10 * time.Minute
	perReqTimeout    = 90 * time.Second
)

// getMaxBodyBytes returns a sensible size limit per resource type
func getMaxBodyBytes(key string) int64 {
	switch {
	case strings.Contains(key, "direct_txt"):
		return 10 * 1024 * 1024 // direct.txt is ~3MB
	default:
		return 5 * 1024 * 1024 // all text/yaml lists are well under 2MB
	}
}

// sanitizeError strips raw URL details from *url.Error to prevent token/credential leakage in logs and errors.
func sanitizeError(err error, safeURL string) error {
	if err == nil {
		return nil
	}
	var urlErr *url.Error
	if errors.As(err, &urlErr) {
		return fmt.Errorf("%s %s: %w", urlErr.Op, safeURL, urlErr.Err)
	}
	return err
}

// Fetcher handles concurrent HTTP GET requests with retries, timeout, and bounds.
type Fetcher struct {
	client  *http.Client
	limiter chan struct{} // bounds max concurrent HTTP downloads
	debug   bool
}

func NewFetcher(concurrency int, debug bool) *Fetcher {
	if concurrency <= 0 {
		concurrency = 6
	}
	tr := &http.Transport{
		Proxy:                 http.ProxyFromEnvironment,
		MaxIdleConns:          50,
		MaxIdleConnsPerHost:   10,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   30 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
		ResponseHeaderTimeout: 60 * time.Second,
	}
	return &Fetcher{
		client: &http.Client{
			Transport: tr,
			Timeout:   perReqTimeout,
		},
		limiter: make(chan struct{}, concurrency),
		debug:   debug,
	}
}

func (f *Fetcher) Fetch(ctx context.Context, rawURL string, maxBytes int64) ([]byte, error) {
	// Parse URL to ensure it is valid http/https
	u, err := url.Parse(rawURL)
	if err != nil {
		return nil, fmt.Errorf("invalid URL: %w", err)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return nil, fmt.Errorf("unsupported scheme %q", u.Scheme)
	}

	// Sanitize URL for logging to prevent credential/token leakage in userinfo, query, or fragment
	cleanU := *u
	cleanU.User = nil
	cleanU.RawQuery = ""
	cleanU.Fragment = ""
	safeURL := cleanU.String()

	select {
	case f.limiter <- struct{}{}:
		defer func() { <-f.limiter }()
	case <-ctx.Done():
		return nil, ctx.Err()
	}

	var lastErr error
	backoff := 1 * time.Second
	const maxRetries = 3

	for attempt := 0; attempt <= maxRetries; attempt++ {
		if attempt > 0 {
			if f.debug {
				log.Printf("[DEBUG] HTTP GET %s retry attempt %d/%d (backoff: %v)", safeURL, attempt, maxRetries, backoff)
			}
			select {
			case <-time.After(backoff):
				backoff *= 2
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}

		start := time.Now()
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
		if err != nil {
			return nil, fmt.Errorf("create request failed: %w", err)
		}
		req.Header.Set("User-Agent", defaultUserAgent)

		resp, err := f.client.Do(req)
		if err != nil {
			lastErr = err
			log.Printf("[RETRY] %s (attempt %d/%d): %v", safeURL, attempt+1, maxRetries+1, err)
			continue
		}

		elapsed := time.Since(start).Milliseconds()

		if resp.StatusCode < 200 || resp.StatusCode >= 300 {
			resp.Body.Close()
			if f.debug {
				bytesReported := resp.ContentLength
				if bytesReported < 0 {
					bytesReported = 0
				}
				log.Printf("[DEBUG] HTTP GET %s -> %d (%d bytes in %dms)", safeURL, resp.StatusCode, bytesReported, elapsed)
				log.Printf("[DEBUG] [HTTP] url=%s status=%d content_length=%d bytes=%d elapsed_ms=%d attempt=%d", safeURL, resp.StatusCode, resp.ContentLength, bytesReported, elapsed, attempt+1)
			}
			lastErr = fmt.Errorf("HTTP status %d (%s)", resp.StatusCode, resp.Status)
			if resp.StatusCode == 429 || (resp.StatusCode >= 500 && resp.StatusCode <= 599) {
				log.Printf("[RETRY] %s (status %d, attempt %d/%d)", safeURL, resp.StatusCode, attempt+1, maxRetries+1)
				continue
			}
			// Non-retriable 4xx
			return nil, fmt.Errorf("fetch %s failed: %w", safeURL, lastErr)
		}

		if maxBytes <= 0 {
			maxBytes = 5 * 1024 * 1024
		}
		lr := io.LimitReader(resp.Body, maxBytes+1)
		data, err := io.ReadAll(lr)
		resp.Body.Close()
		if err != nil {
			lastErr = fmt.Errorf("read body failed: %w", err)
			log.Printf("[RETRY] %s (attempt %d/%d): %v", safeURL, attempt+1, maxRetries+1, err)
			continue
		}
		if len(data) == 0 {
			lastErr = fmt.Errorf("received zero-byte empty response from %s", safeURL)
			log.Printf("[RETRY] %s (empty body, attempt %d/%d)", safeURL, attempt+1, maxRetries+1)
			continue
		}
		if int64(len(data)) > maxBytes {
			return nil, fmt.Errorf("resource %s exceeded maximum allowed size (%d bytes)", safeURL, maxBytes)
		}

		if f.debug {
			log.Printf("[DEBUG] HTTP GET %s -> %d (%d bytes in %dms)", safeURL, resp.StatusCode, len(data), elapsed)
			log.Printf("[DEBUG] [HTTP] url=%s status=%d content_length=%d bytes=%d elapsed_ms=%d attempt=%d", safeURL, resp.StatusCode, resp.ContentLength, len(data), elapsed, attempt+1)
		}

		return data, nil
	}
	return nil, fmt.Errorf("fetch %s failed after %d attempts: %w", safeURL, maxRetries+1, lastErr)
}

// ----------------------------------------------------------------------------
// Transformation Algorithms
// ----------------------------------------------------------------------------

var (
	reIPv4          = regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$`)
	reValidDomain   = regexp.MustCompile(`^[0-9a-zA-Z\.-]+$`)
	reDomainSuffix  = regexp.MustCompile(`^  - DOMAIN(-SUFFIX)?,`)
	reAiPayloadRule = regexp.MustCompile(`^  - (DOMAIN|DOMAIN-SUFFIX|DOMAIN-KEYWORD|IP-CIDR),`)
)

// splitRawLines safely splits an in-memory byte slice into lines without line-length limits.
// It trims a trailing newline so that files ending with a newline do not produce an extra empty element.
func splitRawLines(raw []byte) []string {
	if len(raw) == 0 {
		return nil
	}
	raw = bytes.TrimSuffix(raw, []byte("\n"))
	raw = bytes.TrimSuffix(raw, []byte("\r"))
	if len(raw) == 0 {
		return nil
	}
	parts := bytes.Split(raw, []byte("\n"))
	lines := make([]string, len(parts))
	for i, p := range parts {
		lines[i] = string(bytes.TrimSuffix(p, []byte("\r")))
	}
	return lines
}

func parseGfwList(name string, rawBase64 []byte, debug bool) ([]string, error) {
	// Clean up base64 text (strip spaces, newlines)
	cleaned := bytes.Map(func(r rune) rune {
		if r == '\r' || r == '\n' || r == ' ' || r == '\t' {
			return -1
		}
		return r
	}, rawBase64)

	decoded := make([]byte, base64.StdEncoding.DecodedLen(len(cleaned)))
	n, err := base64.StdEncoding.Decode(decoded, cleaned)
	if err != nil {
		return nil, fmt.Errorf("base64 decode failed: %w", err)
	}
	decoded = decoded[:n]

	rawLines := splitRawLines(decoded)
	totalRaw := len(rawLines)
	var lines []string
	droppedEmptyComment := 0
	droppedBlacklist := 0
	droppedIPv4 := 0
	droppedInvalid := 0
	droppedNoDot := 0

	for _, rawLine := range rawLines {
		line := strings.TrimSpace(rawLine)
		if line == "" || strings.Contains(line, "@@") {
			droppedEmptyComment++
			continue
		}

		// sed 's#!.\+##; s#|##g; s#@##g; s#http:\/\/##; s#https:\/\/##;'
		// Note: sed 's#!.\+##' removes '!' and everything after if '!' is followed by 1+ chars.
		if idx := strings.Index(line, "!"); idx >= 0 {
			if idx+1 < len(line) {
				line = line[:idx]
			}
		}
		line = strings.ReplaceAll(line, "|", "")
		line = strings.ReplaceAll(line, "@", "")
		line = strings.ReplaceAll(line, "https://", "")
		line = strings.ReplaceAll(line, "http://", "")

		// sed '/apple\.com/d; /sina\.cn/d; /sina\.com\.cn/d; /baidu\.com/d; /qq\.com/d'
		if strings.Contains(line, "apple.com") ||
			strings.Contains(line, "sina.cn") ||
			strings.Contains(line, "sina.com.cn") ||
			strings.Contains(line, "baidu.com") ||
			strings.Contains(line, "qq.com") {
			droppedBlacklist++
			continue
		}

		// sed '/^[0-9]\+\.[0-9]\+\.[0-9]\+\.[0-9]\+$/d'
		if reIPv4.MatchString(line) {
			droppedIPv4++
			continue
		}

		// grep '^[0-9a-zA-Z\.-]\+$'
		if !reValidDomain.MatchString(line) {
			droppedInvalid++
			continue
		}

		// grep '\.'
		if !strings.Contains(line, ".") {
			droppedNoDot++
			continue
		}

		// sed 's#^\.\+##'
		line = strings.TrimLeft(line, ".")
		if line == "" {
			droppedEmptyComment++
			continue
		}

		lines = append(lines, line)
	}

	res := sortUniqStrings(lines)
	duplicatesRemoved := len(lines) - len(res)
	if debug {
		log.Printf("[DEBUG] parser %s: parsed %d valid domains from %d lines", name, len(res), totalRaw)
		log.Printf("[DEBUG] [PARSE] %s: raw_lines=%d valid_domains=%d duplicates_removed=%d dropped_empty_comment=%d dropped_blacklist=%d dropped_ipv4=%d dropped_invalid=%d dropped_no_dot=%d",
			name, totalRaw, len(res), duplicatesRemoved, droppedEmptyComment, droppedBlacklist, droppedIPv4, droppedInvalid, droppedNoDot)
	}

	return res, nil
}

// parseFancyssRules processes temp_gfwlist2:
// sed 's/ipset=\/\.//g; s/\/gfwlist//g; /^server/d'
func parseFancyssRules(name string, raw []byte, debug bool) []string {
	rawLines := splitRawLines(raw)
	totalRaw := len(rawLines)
	var lines []string
	droppedServer := 0
	droppedEmpty := 0

	for _, rawLine := range rawLines {
		line := strings.TrimSpace(rawLine)
		if strings.HasPrefix(line, "server") {
			droppedServer++
			continue
		}
		if line == "" {
			droppedEmpty++
			continue
		}
		line = strings.ReplaceAll(line, "ipset=/.", "")
		line = strings.ReplaceAll(line, "/gfwlist", "")
		line = strings.TrimSpace(line)
		if line != "" {
			lines = append(lines, line)
		} else {
			droppedEmpty++
		}
	}

	if debug {
		log.Printf("[DEBUG] parser %s: parsed %d valid domains from %d lines", name, len(lines), totalRaw)
		log.Printf("[DEBUG] [PARSE] %s: raw_lines=%d valid_domains=%d dropped_server=%d dropped_empty=%d",
			name, totalRaw, len(lines), droppedServer, droppedEmpty)
	}
	return lines
}

// parsePlainList processes temp_gfwlist3 (raw lines)
func parsePlainList(name string, raw []byte, debug bool) []string {
	rawLines := splitRawLines(raw)
	totalRaw := len(rawLines)
	var lines []string
	droppedEmpty := 0
	for _, rawLine := range rawLines {
		line := strings.TrimSpace(rawLine)
		if line != "" {
			lines = append(lines, line)
		} else {
			droppedEmpty++
		}
	}
	if debug {
		log.Printf("[DEBUG] parser %s: parsed %d valid domains from %d lines", name, len(lines), totalRaw)
		log.Printf("[DEBUG] [PARSE] %s: raw_lines=%d valid_domains=%d dropped_empty=%d",
			name, totalRaw, len(lines), droppedEmpty)
	}
	return lines
}

// parseMicrosoftList processes temp_gfwlist4:
// grep 'DOMAIN-SUFFIX,' | sed 's/DOMAIN-SUFFIX,//'
func parseMicrosoftList(name string, raw []byte, debug bool) []string {
	rawLines := splitRawLines(raw)
	totalRaw := len(rawLines)
	var lines []string
	droppedNonSuffix := 0
	for _, rawLine := range rawLines {
		line := strings.TrimSpace(rawLine)
		if strings.Contains(line, "DOMAIN-SUFFIX,") {
			parts := strings.Split(line, "DOMAIN-SUFFIX,")
			if len(parts) >= 2 {
				val := strings.TrimSpace(parts[1])
				fields := strings.Split(val, ",")
				domain := strings.TrimSpace(fields[0])
				if domain != "" {
					lines = append(lines, domain)
					continue
				}
			}
		}
		droppedNonSuffix++
	}
	if debug {
		log.Printf("[DEBUG] parser %s: parsed %d valid domains from %d lines", name, len(lines), totalRaw)
		log.Printf("[DEBUG] [PARSE] %s: raw_lines=%d valid_domains=%d dropped_non_suffix=%d",
			name, totalRaw, len(lines), droppedNonSuffix)
	}
	return lines
}

// parseCleanAiDomains processes the 10 clash-ai-rules yaml files:
// grep -E '^  - DOMAIN(-SUFFIX)?,' | sed -E 's/^  - DOMAIN(-SUFFIX)?,//' | sort -u
func parseCleanAiDomains(name string, files [][]byte, debug bool) []string {
	var domains []string
	totalRaw := 0
	for _, f := range files {
		lines := splitRawLines(f)
		totalRaw += len(lines)
		for _, line := range lines {
			if !strings.HasPrefix(line, "  - ") {
				continue
			}
			if reDomainSuffix.MatchString(line) {
				idx := strings.Index(line, ",")
				if idx >= 0 && idx+1 < len(line) {
					dom := strings.TrimSpace(line[idx+1:])
					fields := strings.Split(dom, ",")
					dom = strings.TrimSpace(fields[0])
					if dom != "" {
						domains = append(domains, dom)
					}
				}
			}
		}
	}
	res := sortUniqStrings(domains)
	duplicatesRemoved := len(domains) - len(res)
	if debug {
		log.Printf("[DEBUG] parser %s: parsed %d valid domains from %d lines", name, len(res), totalRaw)
		log.Printf("[DEBUG] [PARSE] %s: input_files=%d raw_lines=%d extracted=%d valid_domains=%d duplicates_removed=%d",
			name, len(files), totalRaw, len(domains), len(res), duplicatesRemoved)
	}
	return res
}

// buildAiYaml processes the 10 clash-ai-rules yaml files for ai.yaml:
// echo "payload:"
// grep -E '^  - (DOMAIN|DOMAIN-SUFFIX|DOMAIN-KEYWORD|IP-CIDR),' | sort -u
func buildAiYaml(files [][]byte) []byte {
	var rules []string
	for _, f := range files {
		for _, line := range splitRawLines(f) {
			line = strings.TrimRight(line, "\r\n")
			if reAiPayloadRule.MatchString(line) {
				rules = append(rules, line)
			}
		}
	}
	rules = sortUniqStrings(rules)

	var buf bytes.Buffer
	buf.WriteString("payload:\n")
	for _, r := range rules {
		buf.WriteString(r)
		buf.WriteString("\n")
	}
	return buf.Bytes()
}

// buildGoogleRulesYaml processes LM-Firefly Google.list:
// echo "payload:"
// awk '!/^PROCESS-NAME/ && !/^##/ && !/^$/ {print "  - "$0}' | sort -u
func buildGoogleRulesYaml(raw []byte) []byte {
	var rules []string
	for _, rawLine := range splitRawLines(raw) {
		line := strings.TrimSpace(rawLine)
		if line == "" || strings.HasPrefix(line, "##") || strings.HasPrefix(line, "PROCESS-NAME") {
			continue
		}
		rules = append(rules, "  - "+line)
	}
	rules = sortUniqStrings(rules)

	var buf bytes.Buffer
	buf.WriteString("payload:\n")
	for _, r := range rules {
		buf.WriteString(r)
		buf.WriteString("\n")
	}
	return buf.Bytes()
}

// filterYamlNonAscii replicates filter-rules.sh:
// Drop any rule line (a payload entry, i.e. a line starting with optional
// whitespace then "- ") that contains a non-ASCII character (> 127).
// Comments and other lines keep non-ASCII.
// If no non-ASCII rule lines are dropped, the original raw bytes are returned unmodified.
func filterYamlNonAscii(filename string, raw []byte, debug bool) []byte {
	if len(raw) == 0 {
		if debug {
			log.Printf("[DEBUG] %s: stripped 0 non-ASCII payload lines", filename)
		}
		return raw
	}
	lines := splitRawLines(raw)
	strippedCount := 0
	var buf bytes.Buffer
	for _, line := range lines {
		trimmed := strings.TrimLeft(line, " \t")
		if strings.HasPrefix(trimmed, "- ") {
			hasNonAscii := false
			for _, r := range line {
				if r > 127 {
					hasNonAscii = true
					break
				}
			}
			if hasNonAscii {
				strippedCount++
				continue
			}
		}
		buf.WriteString(line)
		buf.WriteString("\n")
	}

	if debug {
		log.Printf("[DEBUG] %s: stripped %d non-ASCII payload lines", filename, strippedCount)
	}

	if strippedCount == 0 {
		return raw
	}
	return buf.Bytes()
}

func asciiToLower(s string) string {
	hasUpper := false
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c >= 'A' && c <= 'Z' {
			hasUpper = true
			break
		}
	}
	if !hasUpper {
		return s
	}
	b := make([]byte, len(s))
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c >= 'A' && c <= 'Z' {
			b[i] = c + 32
		} else {
			b[i] = c
		}
	}
	return string(b)
}

func sortUniqStrings(s []string) []string {
	if len(s) == 0 {
		return nil
	}
	sort.Strings(s)
	res := make([]string, 0, len(s))
	last := ""
	for i, v := range s {
		if i == 0 || v != last {
			res = append(res, v)
			last = v
		}
	}
	return res
}

// sortUfSedUniqU replicates:
// sort -uf | sed 's/^\.*//g' | uniq -u
//
// In GNU sort -f (ignore-case) -u (unique keys):
// It sorts elements based on case-folded comparison. When keys fold identically, sort -u keeps
// the first occurrence.
// Then sed 's/^\.*//g' trims leading dots.
// Then uniq -u prints ONLY lines that appear exactly once consecutively.
func sortUfSedUniqU(lines []string) []string {
	if len(lines) == 0 {
		return nil
	}

	type sortItem struct {
		orig  string
		lower string
	}

	items := make([]sortItem, 0, len(lines))
	for _, l := range lines {
		trimmed := strings.TrimSpace(l)
		if trimmed != "" {
			items = append(items, sortItem{
				orig:  trimmed,
				lower: asciiToLower(trimmed),
			})
		}
	}

	// Step 1: Stable sort by lower-case representation (case-insensitive sort)
	sort.SliceStable(items, func(i, j int) bool {
		return items[i].lower < items[j].lower
	})

	// Step 2: sort -u: keep first occurrence of each unique case-folded key
	var uniqueCaseFolded []string
	for i := 0; i < len(items); i++ {
		if i == 0 || items[i].lower != items[i-1].lower {
			uniqueCaseFolded = append(uniqueCaseFolded, items[i].orig)
		}
	}

	// Step 3: sed 's/^\.*//g' -> strip leading dots
	for i, s := range uniqueCaseFolded {
		uniqueCaseFolded[i] = strings.TrimLeft(s, ".")
	}

	// Step 4: uniq -u -> only lines that appear exactly once consecutively
	var result []string
	n := len(uniqueCaseFolded)
	for i := 0; i < n; {
		j := i + 1
		for j < n && uniqueCaseFolded[j] == uniqueCaseFolded[i] {
			j++
		}
		if j == i+1 && uniqueCaseFolded[i] != "" {
			result = append(result, uniqueCaseFolded[i])
		}
		i = j
	}

	return result
}

// differenceWithDirect replicates:
// sort -f list.txt direct.txt direct.txt | uniq -u > output/clean-list.txt
//
// Any item present in direct.txt (case-insensitive) appears at least twice in the input,
// so uniq -u filters it out. It outputs lines in list.txt that do not appear in direct.txt,
// sorted case-insensitively with duplicates removed.
func differenceWithDirect(list []string, direct []string) []string {
	directSet := make(map[string]struct{}, len(direct))
	for _, d := range direct {
		trimmed := strings.TrimSpace(d)
		if trimmed != "" {
			directSet[asciiToLower(trimmed)] = struct{}{}
		}
	}

	type elem struct {
		orig  string
		lower string
	}
	var filtered []elem
	for _, l := range list {
		trimmed := strings.TrimSpace(l)
		if trimmed == "" {
			continue
		}
		low := asciiToLower(trimmed)
		if _, found := directSet[low]; !found {
			filtered = append(filtered, elem{orig: trimmed, lower: low})
		}
	}

	// Sort case-insensitively (LC_ALL=C byte order on lower)
	sort.SliceStable(filtered, func(i, j int) bool {
		return filtered[i].lower < filtered[j].lower
	})

	// uniq -u on the sorted list
	var result []string
	n := len(filtered)
	for i := 0; i < n; {
		j := i + 1
		for j < n && filtered[j].lower == filtered[i].lower {
			j++
		}
		if j == i+1 {
			result = append(result, filtered[i].orig)
		}
		i = j
	}

	return result
}

// parsedSource holds one parsed GFW-list source together with its name for logging.
type parsedSource struct {
	name  string
	lines []string
}

// generateCleanList aggregates and calculates clean-list domains.
func generateCleanList(sources []parsedSource, cleanAi, myProxy, direct []string, debug bool) []string {
	var combined []string
	rawTotal := 0
	var sourceCounts []string
	for _, s := range sources {
		combined = append(combined, s.lines...)
		rawTotal += len(s.lines)
		sourceCounts = append(sourceCounts, fmt.Sprintf("%s=%d", s.name, len(s.lines)))
	}
	combined = append(combined, myProxy...)
	combined = append(combined, cleanAi...)

	rawTotal += len(cleanAi)
	listTxt := sortUfSedUniqU(combined)
	cleanList := differenceWithDirect(listTxt, direct)

	if debug {
		log.Printf("[DEBUG] clean-list: %d raw domains, -%d direct whitelist, +%d custom proxy -> %d unique domains",
			rawTotal, len(direct), len(myProxy), len(cleanList))
		log.Printf("[DEBUG] [AGGREGATE] raw_upstream=%d (%s, clean_ai=%d), custom_proxy=%d, dedup_output=%d, direct_whitelist=%d, direct_subtracted=%d, final_clean_list=%d",
			rawTotal, strings.Join(sourceCounts, ", "), len(cleanAi), len(myProxy), len(listTxt), len(direct), len(listTxt)-len(cleanList), len(cleanList))
	}

	return cleanList
}

func generateDomainRsc(domains []string, debug bool) []byte {
	var domainRscBuf bytes.Buffer
	domainRscBuf.WriteString("/ip dns static remove numbers=[/ip dns static find comment=gfw]\n")
	domainRscBuf.WriteString("/ip dns static\n")
	for _, domain := range domains {
		fmt.Fprintf(&domainRscBuf, "add type=FWD match-subdomain=yes forward-to=clash address-list=gfwlist comment=gfw name=%s\n", domain)
	}
	res := domainRscBuf.Bytes()
	if debug {
		log.Printf("[DEBUG] domain.rsc: generated %d static DNS FWD entries (%d bytes)", len(domains), len(res))
	}
	return res
}

func generateClashGfwList(domains []string, debug bool) []byte {
	var clashGfwBuf bytes.Buffer
	clashGfwBuf.WriteString("payload:\n")
	for _, domain := range domains {
		fmt.Fprintf(&clashGfwBuf, "  - '+.%s'\n", domain)
	}
	res := clashGfwBuf.Bytes()
	if debug {
		log.Printf("[DEBUG] clash-gfw-list: generated %d payload items (%d bytes)", len(domains), len(res))
	}
	return res
}

// ----------------------------------------------------------------------------
// Download Sources
// ----------------------------------------------------------------------------

// GfwlistSource is one upstream GFW-list source and the parser applied to it.
type GfwlistSource struct {
	Name   string `json:"name"`
	URL    string `json:"url"`
	Format string `json:"format"` // gfwlist | fancyss | plain | microsoft
}

// Source is a named upstream URL (ai and google groups).
type Source struct {
	Name string `json:"name"`
	URL  string `json:"url"`
}

// RuleSource is an upstream file copied verbatim to Output (relative to outDir).
type RuleSource struct {
	Name   string `json:"name"`
	URL    string `json:"url"`
	Output string `json:"output"`
}

// Sources is the -sources JSON schema:
// {"gfwlist":[{"name","url","format"}],"ai":[{"name","url"}],"google":[{"name","url"}],"rules":[{"name","url","output"}]}
type Sources struct {
	Gfwlist []GfwlistSource `json:"gfwlist"`
	AI      []Source        `json:"ai"`
	Google  []Source        `json:"google"`
	Rules   []RuleSource    `json:"rules"`
}

var validGfwlistFormats = map[string]bool{
	"gfwlist":   true,
	"fancyss":   true,
	"plain":     true,
	"microsoft": true,
}

// defaultSources mirrors the historical hardcoded download list.
func defaultSources() Sources {
	return Sources{
		Gfwlist: []GfwlistSource{
			{Name: "gfwlist1", URL: "https://raw.githubusercontent.com/gfwlist/gfwlist/master/gfwlist.txt", Format: "gfwlist"},
			{Name: "gfwlist2", URL: "https://raw.githubusercontent.com/hq450/fancyss/master/rules/gfwlist.conf", Format: "fancyss"},
			{Name: "gfwlist3", URL: "https://raw.githubusercontent.com/Loyalsoldier/v2ray-rules-dat/release/gfw.txt", Format: "plain"},
			{Name: "gfwlist4", URL: "https://raw.githubusercontent.com/LM-Firefly/Rules/master/Microsoft.list", Format: "microsoft"},
			{Name: "gfwlist5", URL: "https://raw.githubusercontent.com/Loukky/gfwlist-by-loukky/master/gfwlist.txt", Format: "gfwlist"},
		},
		AI: []Source{
		},
		Google: []Source{
			{Name: "google_list", URL: "https://raw.githubusercontent.com/LM-Firefly/Rules/refs/heads/master/PROXY/Google.list"},
		},
		Rules: []RuleSource{
			{Name: "rule_Telegram", URL: "https://raw.githubusercontent.com/LM-Firefly/Rules/master/Clash-RuleSet-Classical/PROXY/Telegram.yaml", Output: "rules/Telegram.yaml"},
			{Name: "rule_YouTube", URL: "https://raw.githubusercontent.com/LM-Firefly/Rules/master/Clash-RuleSet-Classical/Global-Services/YouTube.yaml", Output: "rules/YouTube.yaml"},
			{Name: "rule_Netflix", URL: "https://raw.githubusercontent.com/LM-Firefly/Rules/master/Clash-RuleSet-Classical/Global-Services/Netflix.yaml", Output: "rules/Netflix.yaml"},
			{Name: "rule_GlobalMedia", URL: "https://raw.githubusercontent.com/LM-Firefly/Rules/master/Clash-RuleSet-Classical/GlobalMedia.yaml", Output: "rules/GlobalMedia.yaml"},
			{Name: "rule_PROXY", URL: "https://raw.githubusercontent.com/LM-Firefly/Rules/master/Clash-RuleSet-Classical/PROXY.yaml", Output: "rules/PROXY.yaml"},
			{Name: "rule_Apple", URL: "https://raw.githubusercontent.com/LM-Firefly/Rules/master/Clash-RuleSet-Classical/Apple.yaml", Output: "rules/Apple.yaml"},
			{Name: "rule_Game", URL: "https://raw.githubusercontent.com/LM-Firefly/Rules/master/Clash-RuleSet-Classical/Game.yaml", Output: "rules/Game.yaml"},
			{Name: "rule_proxy_txt", URL: "https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release/proxy.txt", Output: "rules/proxy.txt"},
			{Name: "rule_lancidr_txt", URL: "https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release/lancidr.txt", Output: "rules/lancidr.txt"},
			{Name: "rule_gfw_txt", URL: "https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release/gfw.txt", Output: "rules/gfw.txt"},
			{Name: "rule_greatfire", URL: "https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release/greatfire.txt", Output: "rules/greatfire.txt"},
			{Name: "rule_direct_txt", URL: "https://raw.githubusercontent.com/Loyalsoldier/clash-rules/release/direct.txt", Output: "rules/direct.txt"},
		},
	}
}

func hasDotDotSegment(p string) bool {
	for _, seg := range strings.Split(p, "/") {
		if seg == ".." {
			return true
		}
	}
	return false
}

// validate rejects duplicate source names and rule outputs escaping the output directory.
func (s Sources) validate() error {
	seen := make(map[string]bool)
	checkName := func(name string) error {
		if seen[name] {
			return fmt.Errorf("duplicate source name %q", name)
		}
		seen[name] = true
		return nil
	}
	for _, src := range s.Gfwlist {
		if err := checkName(src.Name); err != nil {
			return err
		}
		if !validGfwlistFormats[src.Format] {
			return fmt.Errorf("gfwlist source %q: unknown format %q (want gfwlist|fancyss|plain|microsoft)", src.Name, src.Format)
		}
	}
	for _, src := range s.AI {
		if err := checkName(src.Name); err != nil {
			return err
		}
	}
	for _, src := range s.Google {
		if err := checkName(src.Name); err != nil {
			return err
		}
	}
	for _, src := range s.Rules {
		if err := checkName(src.Name); err != nil {
			return err
		}
		if src.Output == "" || filepath.IsAbs(src.Output) || hasDotDotSegment(src.Output) {
			return fmt.Errorf("rule source %q: output %q must be a relative path without .. segments", src.Name, src.Output)
		}
	}
	return nil
}

// loadSources reads the -sources JSON file; an empty path selects the built-in defaults.
func loadSources(path string) (Sources, error) {
	if path == "" {
		return defaultSources(), nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return Sources{}, fmt.Errorf("read sources file %q: %w", path, err)
	}
	var src Sources
	if err := json.Unmarshal(data, &src); err != nil {
		return Sources{}, fmt.Errorf("parse sources file %q: %w", path, err)
	}
	if err := src.validate(); err != nil {
		return Sources{}, fmt.Errorf("sources file %q: %w", path, err)
	}
	return src, nil
}

// ----------------------------------------------------------------------------
// Generator Pipeline
// ----------------------------------------------------------------------------

type Generator struct {
	fetcher    *Fetcher
	outDir     string
	tempParent string
	direct     []string
	myProxy    []string
	sources    Sources
	debug      bool
}

func loadDomainList(flagVal, filePath string, debug bool, listName string) ([]string, error) {
	var domains []string
	if filePath != "" {
		data, err := os.ReadFile(filePath)
		if err != nil {
			return nil, fmt.Errorf("read domains file %q: %w", filePath, err)
		}
		domains = append(domains, parsePlainList(filePath, data, debug)...)
	}
	if flagVal != "" {
		for _, part := range strings.Split(flagVal, ",") {
			trimmed := strings.TrimSpace(part)
			if trimmed != "" {
				domains = append(domains, trimmed)
			}
		}
	}
	if debug {
		log.Printf("[DEBUG] loaded %d domains for %s", len(domains), listName)
	}
	return domains, nil
}

func NewGenerator(outDir, tempParent string, direct, myProxy []string, sources Sources, debug bool) (*Generator, error) {
	absOut, err := filepath.Abs(outDir)
	if err != nil {
		return nil, fmt.Errorf("resolve outDir: %w", err)
	}

	if tempParent == "" {
		tempParent = filepath.Dir(absOut)
	}
	absTempParent, err := filepath.Abs(tempParent)
	if err != nil {
		return nil, fmt.Errorf("resolve tempParent: %w", err)
	}

	return &Generator{
		fetcher:    NewFetcher(8, debug),
		outDir:     absOut,
		tempParent: absTempParent,
		direct:     direct,
		myProxy:    myProxy,
		sources:    sources,
		debug:      debug,
	}, nil
}

func (g *Generator) Run(ctx context.Context) error {
	log.Printf("Starting ros-rules generation -> target: %s", g.outDir)

	// Step 1: Concurrently fetch all upstream resources
	urls := make(map[string]string,
		len(g.sources.Gfwlist)+len(g.sources.AI)+len(g.sources.Google)+len(g.sources.Rules))
	for _, s := range g.sources.Gfwlist {
		urls[s.Name] = s.URL
	}
	for _, s := range g.sources.AI {
		urls[s.Name] = s.URL
	}
	for _, s := range g.sources.Google {
		urls[s.Name] = s.URL
	}
	for _, s := range g.sources.Rules {
		urls[s.Name] = s.URL
	}

	fetched := make(map[string][]byte, len(urls))
	var mu sync.Mutex
	var wg sync.WaitGroup
	errCh := make(chan error, len(urls))

	for key, u := range urls {
		wg.Add(1)
		go func(k, targetURL string) {
			defer wg.Done()
			data, err := g.fetcher.Fetch(ctx, targetURL, getMaxBodyBytes(k))
			if err != nil {
				errCh <- fmt.Errorf("[%s] %w", k, err)
				return
			}
			mu.Lock()
			fetched[k] = data
			mu.Unlock()
		}(key, u)
	}

	wg.Wait()
	close(errCh)

	var errs []string
	for err := range errCh {
		errs = append(errs, err.Error())
	}
	if len(errs) > 0 {
		return fmt.Errorf("failed fetching %d upstream resources:\n  %s", len(errs), strings.Join(errs, "\n  "))
	}

	log.Printf("Successfully fetched all %d upstream resources. Generating rules...", len(urls))
	if g.debug {
		var totalBytes int64
		for _, d := range fetched {
			totalBytes += int64(len(d))
		}
		log.Printf("[DEBUG] [HTTP] fetch complete: %d resources, %d total bytes", len(fetched), totalBytes)
	}

	// Step 2: Transform GFW list sources (dynamic; parser dispatched by format)
	gfwLists := make([]parsedSource, 0, len(g.sources.Gfwlist))
	for _, s := range g.sources.Gfwlist {
		var lines []string
		switch s.Format {
		case "gfwlist":
			parsed, err := parseGfwList(s.Name, fetched[s.Name], g.debug)
			if err != nil {
				return fmt.Errorf("parse %s: %w", s.Name, err)
			}
			lines = parsed
		case "fancyss":
			lines = parseFancyssRules(s.Name, fetched[s.Name], g.debug)
		case "plain":
			lines = parsePlainList(s.Name, fetched[s.Name], g.debug)
		case "microsoft":
			lines = parseMicrosoftList(s.Name, fetched[s.Name], g.debug)
		default:
			return fmt.Errorf("source %s: unknown format %q (want gfwlist|fancyss|plain|microsoft)", s.Name, s.Format)
		}
		gfwLists = append(gfwLists, parsedSource{name: s.Name, lines: lines})
	}

	aiFiles := make([][]byte, 0, len(g.sources.AI))
	for _, s := range g.sources.AI {
		aiFiles = append(aiFiles, fetched[s.Name])
	}
	cleanAi := parseCleanAiDomains("clean-ai", aiFiles, g.debug)

	cleanList := generateCleanList(gfwLists, cleanAi, g.myProxy, g.direct, g.debug)

	log.Printf("Generated clean-list: %d entries", len(cleanList))

	domainRscData := generateDomainRsc(cleanList, g.debug)
	clashGfwData := generateClashGfwList(cleanList, g.debug)

	var cleanListBuf bytes.Buffer
	for _, domain := range cleanList {
		cleanListBuf.WriteString(domain)
		cleanListBuf.WriteString("\n")
	}

	// Generate rules/ai.yaml
	aiYaml := buildAiYaml(aiFiles)

	// Generate rules/google_rules.yaml (one payload section per configured google source)
	var googleRulesYaml []byte
	for _, s := range g.sources.Google {
		googleRulesYaml = append(googleRulesYaml, buildGoogleRulesYaml(fetched[s.Name])...)
	}

	// Prepare map of all output files
	// Top-level: clean-list.txt, domain.rsc, clash-gfw-list.txt
	// rules/: ai.yaml, google_rules.yaml + one passthrough file per configured rule source
	type fileEntry struct {
		path    string // relative to staging root
		content []byte
		isYaml  bool
	}

	var filesToWrite []fileEntry

	filesToWrite = append(filesToWrite, fileEntry{path: "clean-list.txt", content: cleanListBuf.Bytes()})
	filesToWrite = append(filesToWrite, fileEntry{path: "domain.rsc", content: domainRscData})
	filesToWrite = append(filesToWrite, fileEntry{path: "clash-gfw-list.txt", content: clashGfwData, isYaml: true})

	// rules/ files
	filesToWrite = append(filesToWrite, fileEntry{path: "rules/ai.yaml", content: aiYaml, isYaml: true})
	filesToWrite = append(filesToWrite, fileEntry{path: "rules/google_rules.yaml", content: googleRulesYaml, isYaml: true})

	// Passthrough rule files at their configured output paths (yaml outputs get non-ASCII filtering)
	for _, s := range g.sources.Rules {
		filesToWrite = append(filesToWrite, fileEntry{path: s.Output, content: fetched[s.Name], isYaml: strings.HasSuffix(s.Output, ".yaml")})
	}

	// Total files check: 3 top-level + rules/ai.yaml + rules/google_rules.yaml + len(rules) passthrough
	expectedFiles := 5 + len(g.sources.Rules)
	if len(filesToWrite) != expectedFiles {
		return fmt.Errorf("internal inconsistency: expected %d files, got %d", expectedFiles, len(filesToWrite))
	}

	// Step 3: Write to staging directory atomically
	if err := os.MkdirAll(g.tempParent, 0755); err != nil {
		return fmt.Errorf("create temp parent %q: %w", g.tempParent, err)
	}

	stagingDir, err := os.MkdirTemp(g.tempParent, ".lists-tmp-*")
	if err != nil {
		return fmt.Errorf("create staging dir in %q: %w", g.tempParent, err)
	}
	if g.debug {
		log.Printf("[DEBUG] staging in %s", stagingDir)
	}

	// Ensure stagingDir permissions are 0755 so web server (caddy) can traverse it
	if err := os.Chmod(stagingDir, 0755); err != nil {
		_ = os.RemoveAll(stagingDir)
		return fmt.Errorf("chmod staging dir %q: %w", stagingDir, err)
	}
	// Clean up staging on any failure
	defer func() {
		if stagingDir != "" {
			_ = os.RemoveAll(stagingDir)
		}
	}()

	rulesStagingDir := filepath.Join(stagingDir, "rules")
	if err := os.MkdirAll(rulesStagingDir, 0755); err != nil {
		return fmt.Errorf("create staging rules dir: %w", err)
	}

	for _, fe := range filesToWrite {
		content := fe.content
		if fe.isYaml || strings.HasSuffix(fe.path, ".yaml") {
			content = filterYamlNonAscii(fe.path, content, g.debug)
		}

		fullPath := filepath.Join(stagingDir, fe.path)
		// Mode 0644 for all served files
		if err := os.WriteFile(fullPath, content, 0644); err != nil {
			return fmt.Errorf("write %s: %w", fe.path, err)
		}
		if g.debug {
			lineCount := len(splitRawLines(content))
			log.Printf("[DEBUG] [GENERATE] file=%s bytes=%d lines=%d is_yaml=%t", fe.path, len(content), lineCount, fe.isYaml)
		}
	}

	// Verify all output files exist and are non-empty
	for _, fe := range filesToWrite {
		fullPath := filepath.Join(stagingDir, fe.path)
		fi, err := os.Stat(fullPath)
		if err != nil {
			return fmt.Errorf("stat check %s failed: %w", fe.path, err)
		}
		if fi.Size() == 0 {
			return fmt.Errorf("verification failed: %s is empty (0 bytes)", fe.path)
		}
	}
	if g.debug {
		log.Printf("[DEBUG] verified all %d files present and non-empty in staging", len(filesToWrite))
	}

	// Ensure destination directory parent exists
	targetParent := filepath.Dir(g.outDir)
	if err := os.MkdirAll(targetParent, 0755); err != nil {
		return fmt.Errorf("create target parent %q: %w", targetParent, err)
	}

	lfi, err := os.Lstat(g.outDir)
	if err == nil {
		if lfi.Mode()&os.ModeSymlink == 0 {
			return fmt.Errorf("safety check failed: target outDir %q exists as a regular directory, not a symlink; aborting to preserve zero-downtime atomic contract", g.outDir)
		}
	} else if !os.IsNotExist(err) {
		return fmt.Errorf("lstat outDir %q: %w", g.outDir, err)
	}

	// Determine previous target for safe cleanup
	var oldTarget string
	if err == nil {
		oldTarget, _ = os.Readlink(g.outDir)
		if oldTarget != "" && !filepath.IsAbs(oldTarget) {
			oldTarget = filepath.Join(targetParent, oldTarget)
		}
		if g.debug && oldTarget != "" {
			log.Printf("[DEBUG] previous symlink target: %s", oldTarget)
		}
	}

	// Move stagingDir to persistent versioned directory in targetParent
	versionDir := filepath.Join(targetParent, fmt.Sprintf(".lists-v.%d", time.Now().UnixNano()))
	if err := os.Rename(stagingDir, versionDir); err != nil {
		return fmt.Errorf("move staging to version dir %q: %w", versionDir, err)
	}
	stagingDir = "" // now managed under versionDir
	if g.debug {
		log.Printf("[DEBUG] moving to version %s", versionDir)
	}

	// Create temporary symlink pointing to versionDir (use relative name if both under same parent)
	relTarget, err := filepath.Rel(targetParent, versionDir)
	if err != nil {
		relTarget = versionDir
	}

	tempLink := filepath.Join(targetParent, fmt.Sprintf(".lists.new.%d", time.Now().UnixNano()))
	if err := os.Symlink(relTarget, tempLink); err != nil {
		_ = os.RemoveAll(versionDir)
		return fmt.Errorf("create temp symlink %q: %w", tempLink, err)
	}

	// Atomically rename symlink over g.outDir
	swapTimestamp := time.Now().UTC().Format(time.RFC3339Nano)
	if err := os.Rename(tempLink, g.outDir); err != nil {
		_ = os.Remove(tempLink)
		_ = os.RemoveAll(versionDir)
		return fmt.Errorf("atomic rename symlink %q to %q: %w", tempLink, g.outDir, err)
	}
	if g.debug {
		log.Printf("[DEBUG] atomic symlink swap -> %s", relTarget)
		log.Printf("[DEBUG] [DEPLOY] atomic symlink swap completed at %s (target: %s)", swapTimestamp, relTarget)
	}

	// Clean up previous version target if it resides strictly under targetParent and starts with .lists-
	if oldTarget != "" {
		if rel, err := filepath.Rel(targetParent, oldTarget); err == nil && rel != "." && rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
			base := filepath.Base(oldTarget)
			if strings.HasPrefix(base, ".lists-") && base != filepath.Base(versionDir) {
				if fi, err := os.Lstat(oldTarget); err == nil && fi.IsDir() {
					if err := os.RemoveAll(oldTarget); err != nil {
						if g.debug {
							log.Printf("[DEBUG] failed to clean old version %s: %v", oldTarget, err)
						}
					} else {
						if g.debug {
							log.Printf("[DEBUG] cleaned old version %s", oldTarget)
						}
					}
				}
			}
		}
	}

	// Clean up any stale leftover .lists-v.* or .lists-tmp.* versions in targetParent
	entries, _ := os.ReadDir(targetParent)
	for _, e := range entries {
		name := e.Name()
		if name == filepath.Base(versionDir) {
			continue
		}
		if strings.HasPrefix(name, ".lists-v.") || strings.HasPrefix(name, ".lists-tmp.") {
			p := filepath.Join(targetParent, name)
			if fi, err := os.Lstat(p); err == nil && fi.IsDir() {
				if err := os.RemoveAll(p); err != nil {
					if g.debug {
						log.Printf("[DEBUG] failed to clean old version %s: %v", p, err)
					}
				} else {
					if g.debug {
						log.Printf("[DEBUG] cleaned old version %s", p)
					}
				}
			}
		}
	}

	log.Printf("Successfully generated and atomically deployed all %d rule files to %s", len(filesToWrite), g.outDir)
	return nil
}

func main() {
	outDir := flag.String("out", "/etc/nixos/services/caddy/web/lists", "Output directory for generated rule files")
	tempParent := flag.String("temp-parent", "", "Parent directory for staging temporary files (must be on same filesystem as out)")
	timeout := flag.Duration("timeout", defaultTimeout, "Global pipeline execution timeout")
	directDomains := flag.String("direct-domains", "", "Comma-separated list of direct domains")
	directDomainsFile := flag.String("direct-domains-file", "", "Path to file containing direct domains, one per line")
	proxyDomains := flag.String("proxy-domains", "", "Comma-separated list of proxy domains")
	proxyDomainsFile := flag.String("proxy-domains-file", "", "Path to file containing proxy domains, one per line")
	sourcesPath := flag.String("sources", "", "Path to JSON file with download sources (gfwlist/ai/google/rules groups); empty uses built-in defaults")
	debug := flag.Bool("debug", false, "Enable verbose debug logging for systemd journalctl")
	flag.Parse()

	ctx, cancel := context.WithTimeout(context.Background(), *timeout)
	defer cancel()

	direct, err := loadDomainList(*directDomains, *directDomainsFile, *debug, "direct")
	if err != nil {
		log.Fatalf("Load direct domains failed: %v", err)
	}

	proxy, err := loadDomainList(*proxyDomains, *proxyDomainsFile, *debug, "proxy")
	if err != nil {
		log.Fatalf("Load proxy domains failed: %v", err)
	}

	sources, err := loadSources(*sourcesPath)
	if err != nil {
		log.Fatalf("Load sources failed: %v", err)
	}

	gen, err := NewGenerator(*outDir, *tempParent, direct, proxy, sources, *debug)
	if err != nil {
		log.Fatalf("Init generator failed: %v", err)
	}

	if err := gen.Run(ctx); err != nil {
		log.Fatalf("Generation failed: %v", err)
	}
}
