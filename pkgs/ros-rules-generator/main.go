package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
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

// getMaxBodyBytes 返回单个资源的下载体积上限：名字含 direct_txt 的大列表（direct.txt 约 3MB）放宽到 10MiB，其余 5MiB。
// 注意 key 是 -sources 里的源名（生产含 rule_direct_txt），此分支可达，勿按死代码处理。
func getMaxBodyBytes(key string) int64 {
	switch {
	case strings.Contains(key, "direct_txt"):
		return 10 * 1024 * 1024 // direct.txt 类大列表
	default:
		return 5 * 1024 * 1024 // 其余文本/YAML 列表远小于 2MB
	}
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
	reScheme        = regexp.MustCompile(`https?://`)
	reDomainSuffix  = regexp.MustCompile(`^  - DOMAIN(-SUFFIX)?,`)
	reAiPayloadRule = regexp.MustCompile(`^  - (DOMAIN|DOMAIN-SUFFIX|DOMAIN-KEYWORD|IP-CIDR),`)
)

// splitRawLines 按行切分内存内容（无行长限制），剥掉行尾 \r 与末尾换行，避免多出空元素。
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

// parseGfwList 解码 base64 的 GFW-list 并过滤出可用域名：去注释、黑名单站点、纯 IP 与非法行，结果去重排序。
func parseGfwList(name string, rawBase64 []byte, debug bool) ([]string, error) {
	// 清理 base64 文本（去空白与换行）
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
	lines := make([]string, 0, len(rawLines))
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

		// 剥离装饰字符：'!' 后有内容则截断注释段，再去掉 |、@ 与协议前缀
		if idx := strings.Index(line, "!"); idx >= 0 {
			if idx+1 < len(line) {
				line = line[:idx]
			}
		}
		line = strings.ReplaceAll(line, "|", "")
		line = strings.ReplaceAll(line, "@", "")
		line = reScheme.ReplaceAllString(line, "")

		// 剔除国内常见站点（apple/sina/baidu/qq）
		if strings.Contains(line, "apple.com") ||
			strings.Contains(line, "sina.cn") ||
			strings.Contains(line, "sina.com.cn") ||
			strings.Contains(line, "baidu.com") ||
			strings.Contains(line, "qq.com") {
			droppedBlacklist++
			continue
		}

		// 剔除纯 IPv4 行
		if reIPv4.MatchString(line) {
			droppedIPv4++
			continue
		}

		// 仅保留域名合法字符（字母数字 . -）
		if !reValidDomain.MatchString(line) {
			droppedInvalid++
			continue
		}

		// 域名必须含点
		if !strings.Contains(line, ".") {
			droppedNoDot++
			continue
		}

		// 去掉前导点
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

// parseFancyssRules 解析 fancyss dnsmasq 格式：丢弃 server 行，剥掉 ipset=/. 与 /gfwlist 包裹。
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

// parsePlainList 解析纯文本行列表：去首尾空白，丢空行。
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

// parseMicrosoftList 从 Clash 规则文本提取 DOMAIN-SUFFIX 的域名，其余行丢弃。
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

// parseCleanAiDomains 从全部 AI 源 YAML 提取 DOMAIN/DOMAIN-SUFFIX 条目，去重排序。
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

// buildAiYaml 汇总全部 AI 源的 payload 规则行生成 ai.yaml，去重排序。
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

// buildGoogleRulesYaml 将 Google 列表转为 payload 条目（丢弃 ## 注释与 PROCESS-NAME 行），去重排序。
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

// filterYamlNonAscii 去掉 YAML payload 条目行（"- " 开头）中的非 ASCII 行；
// 注释等其他行保留原样，无删除时原样返回原始字节。
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
	for i := range len(s) {
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
	for i := range len(s) {
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

// sortUfSedUniqU 旧管线等价变换：忽略大小写排序去重（保留首个原形）→ 剥前导点 →
// 仅保留变换后恰好出现一次的项（按连续分组判定）。
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

	// 第一步：按小写形态稳定排序（忽略大小写）
	sort.SliceStable(items, func(i, j int) bool {
		return items[i].lower < items[j].lower
	})

	// 第二步：大小写折叠去重，保留首个原形
	uniqueCaseFolded := make([]string, 0, len(items))
	for i := range items {
		if i == 0 || items[i].lower != items[i-1].lower {
			uniqueCaseFolded = append(uniqueCaseFolded, items[i].orig)
		}
	}

	// 第三步：剥掉前导点
	for i, s := range uniqueCaseFolded {
		uniqueCaseFolded[i] = strings.TrimLeft(s, ".")
	}

	// 第四步：仅保留恰好出现一次的项（按连续分组判定）
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

// differenceWithDirect 从 list 中剔除 direct 白名单（忽略大小写），按忽略大小写字节序
// 排序去重后，仅保留恰好出现一次的项。
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
	filtered := make([]elem, 0, len(list))
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

// parsedSource 保存单个已解析的 GFW 域名源（含源名供日志使用）。
type parsedSource struct {
	name  string
	lines []string
}

// generateCleanList 汇总各源与自定义列表，忽略大小写去重排序后扣除 direct 白名单，生成 clean-list。
func generateCleanList(sources []parsedSource, cleanAi, myProxy, cleanIp, direct []string, debug bool) []string {
	rawTotal := 0
	for _, s := range sources {
		rawTotal += len(s.lines)
	}
	rawTotal += len(cleanAi)

	combined := make([]string, 0, rawTotal+len(myProxy)+len(cleanIp))
	for _, s := range sources {
		combined = append(combined, s.lines...)
	}
	combined = append(combined, myProxy...)
	combined = append(combined, cleanIp...)
	combined = append(combined, cleanAi...)

	listTxt := sortUfSedUniqU(combined)
	cleanList := differenceWithDirect(listTxt, direct)

	if debug {
		var sourceCounts []string
		for _, s := range sources {
			sourceCounts = append(sourceCounts, fmt.Sprintf("%s=%d", s.name, len(s.lines)))
		}
		log.Printf("[DEBUG] clean-list: %d raw domains, -%d direct whitelist, +%d custom proxy, +%d clean-ip -> %d unique domains",
			rawTotal, len(direct), len(myProxy), len(cleanIp), len(cleanList))
		log.Printf("[DEBUG] [AGGREGATE] raw_upstream=%d (%s, clean_ai=%d), custom_proxy=%d, clean_ip=%d, dedup_output=%d, direct_whitelist=%d, direct_subtracted=%d, final_clean_list=%d",
			rawTotal, strings.Join(sourceCounts, ", "), len(cleanAi), len(myProxy), len(cleanIp), len(listTxt), len(direct), len(listTxt)-len(cleanList), len(cleanList))
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

// generateDnsmasqForward 生成 dnsmasq 按域名转发行（server=/域名/上游#端口），
// 是 domain.rsc 的 dnsmasq 对应物，供 OpenWrt 等 dnsmasq 站点使用；每个上游一份输出文件。
func generateDnsmasqForward(domains []string, target string, port int, debug bool) []byte {
	var buf bytes.Buffer
	fmt.Fprintf(&buf, "# generated by ros-rules-generator - do not edit\n")
	fmt.Fprintf(&buf, "# forward listed domains to %s#%d\n", target, port)
	for _, domain := range domains {
		fmt.Fprintf(&buf, "server=/%s/%s#%d\n", domain, target, port)
	}
	res := buf.Bytes()
	if debug {
		log.Printf("[DEBUG] dnsmasq forwarding: generated %d server= entries (%d bytes)", len(domains), len(res))
	}
	return res
}

// telegramCidrURL 是 Telegram 官方公开 CIDR 列表（IPv4+IPv6），与 RouterOS telegram 地址列表更新脚本同源。
const telegramCidrURL = "https://core.telegram.org/resources/cidr.txt"

// telegramFetchKey 是并发拉取表里的保留键，前导下划线避免与用户配置的源名冲突。
const telegramFetchKey = "__telegram_cidr__"

// parseTelegramCidrs 把官方 cidr.txt 拆成 IPv4 / IPv6 两组，丢空行与 # 注释；
// 只取地址段，不做端口/协议特判（Telegram 语音/UDP 处理不在范围内）。
func parseTelegramCidrs(raw []byte, debug bool) (v4, v6 []string) {
	for _, line := range splitRawLines(raw) {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if strings.Contains(line, ":") {
			v6 = append(v6, line)
		} else {
			v4 = append(v4, line)
		}
	}
	if debug {
		log.Printf("[DEBUG] telegram: parsed %d IPv4 + %d IPv6 CIDRs", len(v4), len(v6))
	}
	return v4, v6
}

// generateTelegramRsc 生成 RouterOS telegram 地址列表导入：条目带 comment=telegram-official-managed，
// 换代时只清理托管条目，手工标记 telegram-manual-* 的条目得以保留。
func generateTelegramRsc(v4, v6 []string, debug bool) []byte {
	var buf bytes.Buffer
	buf.WriteString("/ip firewall address-list remove [find where list=telegram comment=telegram-official-managed]\n")
	buf.WriteString("/ip firewall address-list\n")
	for _, cidr := range v4 {
		fmt.Fprintf(&buf, "add list=telegram address=%s comment=telegram-official-managed\n", cidr)
	}
	for _, cidr := range v6 {
		fmt.Fprintf(&buf, "add list=telegram address=%s comment=telegram-official-managed\n", cidr)
	}
	res := buf.Bytes()
	if debug {
		log.Printf("[DEBUG] telegram.rsc: generated %d address-list entries (%d bytes)", len(v4)+len(v6), len(res))
	}
	return res
}

// generateTelegramMihomo 生成 telegram 地址段的 mihomo rule-provider payload（telegram.rsc 的对应物）。
func generateTelegramMihomo(v4, v6 []string, debug bool) []byte {
	var buf bytes.Buffer
	buf.WriteString("payload:\n")
	for _, cidr := range v4 {
		fmt.Fprintf(&buf, "  - IP-CIDR,%s,no-resolve\n", cidr)
	}
	for _, cidr := range v6 {
		fmt.Fprintf(&buf, "  - IP-CIDR6,%s,no-resolve\n", cidr)
	}
	res := buf.Bytes()
	if debug {
		log.Printf("[DEBUG] telegram-mihomo.yaml: generated %d payload items (%d bytes)", len(v4)+len(v6), len(res))
	}
	return res
}

func generateClashDomainList(domains []string, listName string, debug bool) []byte {
	var clashGfwBuf bytes.Buffer
	clashGfwBuf.WriteString("payload:\n")
	for _, domain := range domains {
		fmt.Fprintf(&clashGfwBuf, "  - '+.%s'\n", domain)
	}
	res := clashGfwBuf.Bytes()
	if debug {
		log.Printf("[DEBUG] %s: generated %d payload items (%d bytes)", listName, len(domains), len(res))
	}
	return res
}

func generateClashGfwList(domains []string, debug bool) []byte {
	return generateClashDomainList(domains, "clash-gfw-list", debug)
}

// ----------------------------------------------------------------------------
// TMDB DNS 映射（思路源自 myzhongqiang/DNS-TMDB，解析通道换大厂 DoH 并加硬校验）
// 管线：大厂 DoH 取真实 A 记录（ECS 提示仅影响 CDN 选边）→ 候选 IP 逐一 TLS SNI 握手、
// 按证书 SAN 证明「该 IP 在服务该域名」→ 生成 tmdb.rsc 供 RouterOS 静态映射。
// ----------------------------------------------------------------------------

const (
	defaultTmdbECS = "" // 默认不带 ECS 提示（空 = 关闭）；生产由模块显式传 -tmdb-edns-subnet
	tmdbMaxIPs     = 4  // 每域名最多保留的已验证 IP
	tmdbVerifyTO   = 5 * time.Second
	tmdbDohTO      = 15 * time.Second
)

// dohEndpoints 只用大厂 DoH JSON API。
type dohEndpoint struct{ url, accept string }

var dohEndpoints = []dohEndpoint{
	{"https://dns.google/resolve", "application/dns-json"},
	{"https://cloudflare-dns.com/dns-query", "application/dns-json"},
}

// tmdbVerifyPort 供测试替换（生产恒为 443）。
var tmdbVerifyPort = "443"

// dohQueryA 向单个 DoH JSON 端点查询域名的 A 记录（可附 ECS 提示）。
func dohQueryA(ctx context.Context, endpoint, accept, domain, ecs string) ([]string, error) {
	q := endpoint + "?name=" + url.QueryEscape(domain) + "&type=A"
	if ecs != "" {
		q += "&edns_client_subnet=" + url.QueryEscape(ecs)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, q, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", accept)
	resp, err := (&http.Client{Timeout: tmdbDohTO}).Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<16))
	if err != nil {
		return nil, err
	}
	return parseDoHAnswers(domain, raw)
}

// parseDoHAnswers 从 DoH JSON 响应提取 IPv4 A 记录（Google/Cloudflare 共同 schema）。
func parseDoHAnswers(domain string, raw []byte) ([]string, error) {
	var payload struct {
		Status int `json:"Status"`
		Answer []struct {
			Type int    `json:"type"`
			Data string `json:"data"`
		} `json:"Answer"`
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, fmt.Errorf("decode DoH answer for %s: %w", domain, err)
	}
	if payload.Status != 0 {
		return nil, fmt.Errorf("DoH %s: rcode %d", domain, payload.Status)
	}
	var ips []string
	for _, a := range payload.Answer {
		if a.Type != 1 { // A only
			continue
		}
		ip := net.ParseIP(a.Data)
		if ip == nil || ip.To4() == nil {
			continue
		}
		ips = append(ips, ip.To4().String())
	}
	return ips, nil
}

// verifyTmdbIP 以 SNI=domain 对 ip:443 做 TLS 握手并校验证书 SAN，证明该 IP 真的在服务该域名。
func verifyTmdbIP(ctx context.Context, ip, domain string) error {
	dialer := &tls.Dialer{
		NetDialer: &net.Dialer{Timeout: tmdbVerifyTO},
		Config: &tls.Config{
			ServerName: domain,
			// 不做 CA 链校验：这里要证明的是「IP 在服务该域名」而非「谁签的名」，
			// 域名归属由下面的 SAN 校验显式确认。
			InsecureSkipVerify: true,
			MinVersion:         tls.VersionTLS12,
		},
	}
	conn, err := dialer.DialContext(ctx, "tcp", net.JoinHostPort(ip, tmdbVerifyPort))
	if err != nil {
		return err
	}
	defer conn.Close()
	state := conn.(*tls.Conn).ConnectionState()
	if len(state.PeerCertificates) == 0 {
		return errors.New("no peer certificate")
	}
	return state.PeerCertificates[0].VerifyHostname(domain)
}

// resolveTmdb 经全部 DoH 端点解析每个域名，仅保留通过 TLS SNI 校验的 IP。
// 任一域名零验证 IP 即整轮失败：静默缺口会复发 DNS 污染症状，原子部署契约保住旧列表在线。
func (g *Generator) resolveTmdb(ctx context.Context) (map[string][]string, error) {
	verified := make(map[string][]string, len(g.tmdbDomains))
	for _, domain := range sortUniqStrings(g.tmdbDomains) {
		var mu sync.Mutex
		var wg sync.WaitGroup
		var candidates []string
		for _, ep := range dohEndpoints {
			wg.Add(1)
			go func(u, accept string) {
				defer wg.Done()
				ips, err := dohQueryA(ctx, u, accept, domain, g.tmdbECS)
				if err != nil {
					log.Printf("tmdb dns: %s via %s: %v", domain, u, err)
					return
				}
				mu.Lock()
				candidates = append(candidates, ips...)
				mu.Unlock()
			}(ep.url, ep.accept)
		}
		wg.Wait()

		var vmu sync.Mutex
		var vwg sync.WaitGroup
		var good []string
		for _, ip := range sortUniqStrings(candidates) {
			vwg.Add(1)
			go func(ip string) {
				defer vwg.Done()
				if err := verifyTmdbIP(ctx, ip, domain); err != nil {
					log.Printf("tmdb verify: %s (%s) rejected: %v", ip, domain, err)
					return
				}
				vmu.Lock()
				good = append(good, ip)
				vmu.Unlock()
			}(ip)
		}
		vwg.Wait()

		if len(good) == 0 {
			return nil, fmt.Errorf("domain %s: no verified IP among %d candidates", domain, len(candidates))
		}
		good = sortUniqStrings(good)
		if len(good) > tmdbMaxIPs {
			good = good[:tmdbMaxIPs]
		}
		verified[domain] = good
		log.Printf("tmdb dns: %s -> %s", domain, strings.Join(good, ","))
	}
	return verified, nil
}

// generateTmdbRsc 生成 RouterOS DNS 静态 A 映射（comment=tmdb，重复导入可整体替换）。
func generateTmdbRsc(verified map[string][]string, debug bool) []byte {
	var buf bytes.Buffer
	buf.WriteString("/ip dns static remove numbers=[/ip dns static find comment=tmdb]\n")
	buf.WriteString("/ip dns static\n")
	domains := make([]string, 0, len(verified))
	for d := range verified {
		domains = append(domains, d)
	}
	sort.Strings(domains)
	entries := 0
	for _, domain := range domains {
		for _, ip := range verified[domain] {
			fmt.Fprintf(&buf, "add name=%s address=%s type=A comment=tmdb\n", domain, ip)
			entries++
		}
	}
	res := buf.Bytes()
	if debug {
		log.Printf("[DEBUG] tmdb.rsc: generated %d static DNS A entries (%d bytes)", entries, len(res))
	}
	return res
}

// ----------------------------------------------------------------------------
// Download Sources
// ----------------------------------------------------------------------------

// GfwlistSource 是一个 GFW-list 上游源及其解析格式（format 决定用哪个解析器）。
type GfwlistSource struct {
	Name   string `json:"name"`
	URL    string `json:"url"`
	Format string `json:"format"` // gfwlist | fancyss | plain | microsoft
}

// Source 是一个命名上游 URL（ai / google 组）。
type Source struct {
	Name string `json:"name"`
	URL  string `json:"url"`
}

// RuleSource 是按原样透传到 Output（相对 outDir）的上游文件。
type RuleSource struct {
	Name   string `json:"name"`
	URL    string `json:"url"`
	Output string `json:"output"`
}

// Sources 是 -sources JSON 结构：
// {"gfwlist":[{"name","url","format"}],"ai":[{"name","url"}],"google":[{"name","url"}],"rules":[{"name","url","output"}]}
type Sources struct {
	Gfwlist []GfwlistSource `json:"gfwlist"`
	AI      []Source        `json:"ai"`
	Google  []Source        `json:"google"`
	Rules   []RuleSource    `json:"rules"`
}

// DnsmasqForward 是 -dnsmasq-forwards-file JSON 数组里的一条命名上游；
// name=default 沿用 domain.dnsmasq.conf 文件名。
type DnsmasqForward struct {
	Name   string `json:"name"`
	Target string `json:"target"`
	Port   int    `json:"port"`
}

var validDnsmasqName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]*$`)

func (f DnsmasqForward) outputPath() string {
	if f.Name == "default" {
		return "domain.dnsmasq.conf"
	}
	return "domain.dnsmasq." + f.Name + ".conf"
}

func validDnsmasqTarget(target string) bool {
	if net.ParseIP(target) != nil {
		return true
	}
	if len(target) == 0 || len(target) > 253 {
		return false
	}
	host := strings.TrimSuffix(target, ".")
	for _, label := range strings.Split(host, ".") {
		if len(label) == 0 || len(label) > 63 || label[0] == '-' || label[len(label)-1] == '-' {
			return false
		}
		for i := range len(label) {
			c := label[i]
			if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '-') {
				return false
			}
		}
	}
	return true
}

func validateDnsmasqForwards(forwards []DnsmasqForward) error {
	seen := make(map[string]bool, len(forwards))
	for _, f := range forwards {
		if !validDnsmasqName.MatchString(f.Name) {
			return fmt.Errorf("dnsmasq forward name %q must be an ASCII alphanumeric name containing only letters, digits, underscores or hyphens", f.Name)
		}
		if seen[f.Name] {
			return fmt.Errorf("duplicate dnsmasq forward name %q", f.Name)
		}
		seen[f.Name] = true
		if !validDnsmasqTarget(f.Target) {
			return fmt.Errorf("dnsmasq forward %q: invalid target %q (want an IP address or hostname)", f.Name, f.Target)
		}
		if f.Port < 1 || f.Port > 65535 {
			return fmt.Errorf("dnsmasq forward %q: port %d out of range 1-65535", f.Name, f.Port)
		}
	}
	return nil
}

func loadDnsmasqForwards(path string) ([]DnsmasqForward, error) {
	if path == "" {
		return nil, nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read dnsmasq forwards file %q: %w", path, err)
	}
	var forwards []DnsmasqForward
	if err := json.Unmarshal(data, &forwards); err != nil {
		return nil, fmt.Errorf("parse dnsmasq forwards file %q: %w", path, err)
	}
	if forwards == nil {
		return nil, fmt.Errorf("dnsmasq forwards file %q: expected a JSON array", path)
	}
	return forwards, nil
}

var validGfwlistFormats = map[string]bool{
	"gfwlist":   true,
	"fancyss":   true,
	"plain":     true,
	"microsoft": true,
}

// defaultSources 返回 -sources 缺省时的空内置源。上游 URL 属部署私有配置，
// 由 NixOS 模块经 -sources JSON 传入，故默认刻意留空。
func defaultSources() Sources {
	return Sources{
		Gfwlist: []GfwlistSource{},
		AI:      []Source{},
		Google:  []Source{},
		Rules:   []RuleSource{},
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

// validate 拒绝重名源与逃逸输出目录的 rule output。
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

// loadSources 读取 -sources JSON 文件；空路径选择空内置默认。
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
	fetcher         *Fetcher
	outDir          string
	tempParent      string
	direct          []string
	myProxy         []string
	cleanIp         []string
	tmdbDomains     []string
	tmdbECS         string
	dnsmasqForwards []DnsmasqForward
	sources         Sources
	debug           bool
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

func NewGenerator(outDir, tempParent string, direct, myProxy, cleanIp, tmdbDomains []string, tmdbECS string, dnsmasqForwards []DnsmasqForward, sources Sources, debug bool) (*Generator, error) {
	if err := validateDnsmasqForwards(dnsmasqForwards); err != nil {
		return nil, err
	}
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
		fetcher:         NewFetcher(8, debug),
		outDir:          absOut,
		tempParent:      absTempParent,
		direct:          direct,
		myProxy:         myProxy,
		cleanIp:         cleanIp,
		tmdbDomains:     tmdbDomains,
		tmdbECS:         tmdbECS,
		dnsmasqForwards: dnsmasqForwards,
		sources:         sources,
		debug:           debug,
	}, nil
}

// fileEntry 是一个待写出的产物文件，path 相对 staging 根目录。
type fileEntry struct {
	path    string // 相对 staging 根
	content []byte
	isYaml  bool
}

// baseOutputPaths 是固定产物文件名清单（不含 dnsmasq 变体、规则透传与可选 TMDB 映射）。
var baseOutputPaths = []string{
	"clean-list.txt",
	"domain.rsc",
	"telegram.rsc",
	"telegram-mihomo.yaml",
	"clash-gfw-list.txt",
	"rules/clean-ip.txt",
	"rules/ai.yaml",
	"rules/google_rules.yaml",
}

// fetchUpstream 并发拉取全部上游资源（各源 + Telegram 官方 CIDR）；
// 任一失败即聚合报错并中止，避免半套产物上线。
func (g *Generator) fetchUpstream(ctx context.Context) (map[string][]byte, error) {
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
	// Telegram official CIDR list (transformed into telegram.rsc + telegram-mihomo.yaml)
	urls[telegramFetchKey] = telegramCidrURL

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
		return nil, fmt.Errorf("failed fetching %d upstream resources:\n  %s", len(errs), strings.Join(errs, "\n  "))
	}

	log.Printf("Successfully fetched all %d upstream resources. Generating rules...", len(urls))
	if g.debug {
		var totalBytes int64
		for _, d := range fetched {
			totalBytes += int64(len(d))
		}
		log.Printf("[DEBUG] [HTTP] fetch complete: %d resources, %d total bytes", len(fetched), totalBytes)
	}
	return fetched, nil
}

// buildCleanList 解析全部 GFW 域名源（按 format 分派解析器）与 AI 源，汇总生成 clean-list；
// 返回 clean-list 与 AI 源原始内容（ai.yaml 复用，避免二次解析）。
func (g *Generator) buildCleanList(fetched map[string][]byte) ([]string, [][]byte, error) {
	gfwLists := make([]parsedSource, 0, len(g.sources.Gfwlist))
	for _, s := range g.sources.Gfwlist {
		var lines []string
		switch s.Format {
		case "gfwlist":
			parsed, err := parseGfwList(s.Name, fetched[s.Name], g.debug)
			if err != nil {
				return nil, nil, fmt.Errorf("parse %s: %w", s.Name, err)
			}
			lines = parsed
		case "fancyss":
			lines = parseFancyssRules(s.Name, fetched[s.Name], g.debug)
		case "plain":
			lines = parsePlainList(s.Name, fetched[s.Name], g.debug)
		case "microsoft":
			lines = parseMicrosoftList(s.Name, fetched[s.Name], g.debug)
		default:
			return nil, nil, fmt.Errorf("source %s: unknown format %q (want gfwlist|fancyss|plain|microsoft)", s.Name, s.Format)
		}
		gfwLists = append(gfwLists, parsedSource{name: s.Name, lines: lines})
	}

	aiFiles := make([][]byte, 0, len(g.sources.AI))
	for _, s := range g.sources.AI {
		aiFiles = append(aiFiles, fetched[s.Name])
	}
	cleanAi := parseCleanAiDomains("clean-ai", aiFiles, g.debug)

	cleanList := generateCleanList(gfwLists, cleanAi, g.myProxy, g.cleanIp, g.direct, g.debug)

	log.Printf("Generated clean-list: %d entries", len(cleanList))
	return cleanList, aiFiles, nil
}

// renderTmdbRsc 渲染 TMDB DNS 静态映射（tmdbDomains 为空则关闭并返回 nil）。
func (g *Generator) renderTmdbRsc(ctx context.Context) ([]byte, error) {
	if len(g.tmdbDomains) == 0 {
		return nil, nil
	}
	verified, err := g.resolveTmdb(ctx)
	if err != nil {
		return nil, fmt.Errorf("tmdb dns mapping: %w", err)
	}
	return generateTmdbRsc(verified, g.debug), nil
}

// assembleFiles 装配全部产物清单：clean-list/domain/clash 清单、dnsmasq 变体、
// Telegram 双格式、TMDB 映射与规则透传文件。
func (g *Generator) assembleFiles(ctx context.Context, fetched map[string][]byte, cleanList []string, aiFiles [][]byte) ([]fileEntry, error) {
	domainRscData := generateDomainRsc(cleanList, g.debug)
	clashGfwData := generateClashGfwList(cleanList, g.debug)
	cleanIpData := generateClashDomainList(sortUfSedUniqU(g.cleanIp), "rules/clean-ip.txt", g.debug)

	var cleanListBuf bytes.Buffer
	for _, domain := range cleanList {
		cleanListBuf.WriteString(domain)
		cleanListBuf.WriteString("\n")
	}

	// 生成 rules/ai.yaml
	aiYaml := buildAiYaml(aiFiles)

	// 生成 rules/google_rules.yaml（每个 google 源一个 payload 段）
	var googleRulesYaml []byte
	for _, s := range g.sources.Google {
		googleRulesYaml = append(googleRulesYaml, buildGoogleRulesYaml(fetched[s.Name])...)
	}

	// TMDB DNS 映射（可选；失败即中止整轮生成）
	tmdbRsc, err := g.renderTmdbRsc(ctx)
	if err != nil {
		return nil, err
	}

	// 装配产物清单：顶层 clean-list.txt、domain.rsc、clash-gfw-list.txt 等；
	// rules/ 下 ai.yaml、google_rules.yaml 与每个规则透传源一个文件
	filesToWrite := make([]fileEntry, 0, len(baseOutputPaths)+len(g.dnsmasqForwards)+len(g.sources.Rules)+1)

	filesToWrite = append(filesToWrite, fileEntry{path: "clean-list.txt", content: cleanListBuf.Bytes()})
	filesToWrite = append(filesToWrite, fileEntry{path: "domain.rsc", content: domainRscData})
	for _, forward := range g.dnsmasqForwards {
		filesToWrite = append(filesToWrite, fileEntry{path: forward.outputPath(), content: generateDnsmasqForward(cleanList, forward.Target, forward.Port, g.debug)})
	}
	telegramV4, telegramV6 := parseTelegramCidrs(fetched[telegramFetchKey], g.debug)
	filesToWrite = append(filesToWrite,
		fileEntry{path: "telegram.rsc", content: generateTelegramRsc(telegramV4, telegramV6, g.debug)},
		fileEntry{path: "telegram-mihomo.yaml", content: generateTelegramMihomo(telegramV4, telegramV6, g.debug), isYaml: true})
	filesToWrite = append(filesToWrite, fileEntry{path: "clash-gfw-list.txt", content: clashGfwData, isYaml: true})

	if tmdbRsc != nil {
		filesToWrite = append(filesToWrite, fileEntry{path: "tmdb.rsc", content: tmdbRsc})
	}

	// rules/ 产物
	filesToWrite = append(filesToWrite, fileEntry{path: "rules/clean-ip.txt", content: cleanIpData, isYaml: true})
	filesToWrite = append(filesToWrite, fileEntry{path: "rules/ai.yaml", content: aiYaml, isYaml: true})
	filesToWrite = append(filesToWrite, fileEntry{path: "rules/google_rules.yaml", content: googleRulesYaml, isYaml: true})

	// 规则透传文件写到配置的输出路径（.yaml 输出做非 ASCII 过滤）
	for _, s := range g.sources.Rules {
		filesToWrite = append(filesToWrite, fileEntry{path: s.Output, content: fetched[s.Name], isYaml: strings.HasSuffix(s.Output, ".yaml")})
	}

	return filesToWrite, nil
}

// checkOutputs 校验产物清单：先与期望文件名集合比对（集合比较替代魔法数计数），
// 再拒绝非法路径、重复路径与文件/目录冲突；全部通过后才允许触碰 staging。
func (g *Generator) checkOutputs(filesToWrite []fileEntry, tmdbEnabled bool) error {
	expected := make(map[string]struct{}, len(baseOutputPaths)+len(g.dnsmasqForwards)+len(g.sources.Rules)+1)
	for _, p := range baseOutputPaths {
		expected[p] = struct{}{}
	}
	for _, f := range g.dnsmasqForwards {
		expected[f.outputPath()] = struct{}{}
	}
	for _, s := range g.sources.Rules {
		expected[s.Output] = struct{}{}
	}
	if tmdbEnabled {
		expected["tmdb.rsc"] = struct{}{}
	}

	actual := make(map[string]struct{}, len(filesToWrite))
	for _, fe := range filesToWrite {
		actual[fe.path] = struct{}{}
	}
	var missing, unexpected []string
	for p := range expected {
		if _, ok := actual[p]; !ok {
			missing = append(missing, p)
		}
	}
	for p := range actual {
		if _, ok := expected[p]; !ok {
			unexpected = append(unexpected, p)
		}
	}
	if len(missing) > 0 || len(unexpected) > 0 {
		sort.Strings(missing)
		sort.Strings(unexpected)
		return fmt.Errorf("internal inconsistency: output set mismatch (missing: %s, unexpected: %s)",
			strings.Join(missing, ", "), strings.Join(unexpected, ", "))
	}

	// 触碰 staging 前先拒绝重复/非法/冲突路径
	paths := make(map[string]bool, len(filesToWrite))
	for _, fe := range filesToWrite {
		path := filepath.Clean(fe.path)
		if fe.path == "" || filepath.IsAbs(fe.path) || hasDotDotSegment(fe.path) || path == "." || path == ".." || strings.HasPrefix(path, ".."+string(os.PathSeparator)) {
			return fmt.Errorf("invalid output path %q", fe.path)
		}
		if paths[path] {
			return fmt.Errorf("duplicate output path %q", path)
		}
		paths[path] = true
	}
	for path := range paths {
		for parent := filepath.Dir(path); parent != "."; parent = filepath.Dir(parent) {
			if paths[parent] {
				return fmt.Errorf("output path %q conflicts with file %q", path, parent)
			}
		}
	}
	return nil
}

// prepareStaging 在 tempParent 下建 staging 目录（权限 0755 保证 web 服务可穿越）与 rules 子目录。
func (g *Generator) prepareStaging() (string, error) {
	if err := os.MkdirAll(g.tempParent, 0755); err != nil {
		return "", fmt.Errorf("create temp parent %q: %w", g.tempParent, err)
	}

	stagingDir, err := os.MkdirTemp(g.tempParent, ".lists-tmp-*")
	if err != nil {
		return "", fmt.Errorf("create staging dir in %q: %w", g.tempParent, err)
	}
	if g.debug {
		log.Printf("[DEBUG] staging in %s", stagingDir)
	}

	if err := os.Chmod(stagingDir, 0755); err != nil {
		_ = os.RemoveAll(stagingDir)
		return "", fmt.Errorf("chmod staging dir %q: %w", stagingDir, err)
	}

	rulesStagingDir := filepath.Join(stagingDir, "rules")
	if err := os.MkdirAll(rulesStagingDir, 0755); err != nil {
		_ = os.RemoveAll(stagingDir)
		return "", fmt.Errorf("create staging rules dir: %w", err)
	}
	return stagingDir, nil
}

// writeOutputs 把产物写入 staging（yaml 产物做非 ASCII payload 过滤，文件统一 0644）。
func (g *Generator) writeOutputs(stagingDir string, filesToWrite []fileEntry) error {
	for _, fe := range filesToWrite {
		content := fe.content
		if fe.isYaml || strings.HasSuffix(fe.path, ".yaml") {
			content = filterYamlNonAscii(fe.path, content, g.debug)
		}

		fullPath := filepath.Join(stagingDir, fe.path)
		// 供 web 服务的文件统一 0644
		if err := os.WriteFile(fullPath, content, 0644); err != nil {
			return fmt.Errorf("write %s: %w", fe.path, err)
		}
		if g.debug {
			lineCount := len(splitRawLines(content))
			log.Printf("[DEBUG] [GENERATE] file=%s bytes=%d lines=%d is_yaml=%t", fe.path, len(content), lineCount, fe.isYaml)
		}
	}
	return nil
}

// verifyOutputs 自校验：staging 中每个产物必须存在且非空，防止空文件换链上线。
func (g *Generator) verifyOutputs(stagingDir string, filesToWrite []fileEntry) error {
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
	return nil
}

// deployStaging 原子上线 staging：移入版本目录、软链一次换名覆盖 outDir，并清掉旧版本与残留。
// 成功返回后 staging 由版本目录接管；失败时 staging 仍归调用方清理。
func (g *Generator) deployStaging(stagingDir string, fileCount int) error {
	// 确保目标父目录存在
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

	// 已有软链时解析旧版本目标用于事后清理
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

	// staging 移入持久版本目录
	versionDir := filepath.Join(targetParent, fmt.Sprintf(".lists-v.%d", time.Now().UnixNano()))
	if err := os.Rename(stagingDir, versionDir); err != nil {
		return fmt.Errorf("move staging to version dir %q: %w", versionDir, err)
	}
	if g.debug {
		log.Printf("[DEBUG] moving to version %s", versionDir)
	}

	// 临时软链指向版本目录（同父目录下用相对目标）
	relTarget, err := filepath.Rel(targetParent, versionDir)
	if err != nil {
		relTarget = versionDir
	}

	tempLink := filepath.Join(targetParent, fmt.Sprintf(".lists.new.%d", time.Now().UnixNano()))
	if err := os.Symlink(relTarget, tempLink); err != nil {
		_ = os.RemoveAll(versionDir)
		return fmt.Errorf("create temp symlink %q: %w", tempLink, err)
	}

	// 软链原子改名覆盖 outDir（零停机换链）
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

	g.cleanupVersions(targetParent, oldTarget, filepath.Base(versionDir))

	log.Printf("Successfully generated and atomically deployed all %d rule files to %s", fileCount, g.outDir)
	return nil
}

// cleanupVersions 清理上一软链指向的旧版本目录，以及 targetParent 下残留的
// .lists-v.* / .lists-tmp.* 版本目录；keepBase 是本次保留的版本目录名。
func (g *Generator) cleanupVersions(targetParent, oldTarget, keepBase string) {
	if oldTarget != "" {
		if rel, err := filepath.Rel(targetParent, oldTarget); err == nil && rel != "." && rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
			base := filepath.Base(oldTarget)
			if strings.HasPrefix(base, ".lists-") && base != keepBase {
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

	entries, _ := os.ReadDir(targetParent)
	for _, e := range entries {
		name := e.Name()
		if name == keepBase {
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
}

// Run 执行整条生成管线：拉取源 → 生成 clean-list → 装配产物（含 TMDB 映射）→ 产物校验
// → 写出 staging → 自校验 → 原子上线。任一阶段失败即中止，旧列表保持在线。
func (g *Generator) Run(ctx context.Context) error {
	log.Printf("Starting ros-rules generation -> target: %s", g.outDir)

	fetched, err := g.fetchUpstream(ctx)
	if err != nil {
		return err
	}

	cleanList, aiFiles, err := g.buildCleanList(fetched)
	if err != nil {
		return err
	}

	filesToWrite, err := g.assembleFiles(ctx, fetched, cleanList, aiFiles)
	if err != nil {
		return err
	}

	if err := g.checkOutputs(filesToWrite, len(g.tmdbDomains) > 0); err != nil {
		return err
	}

	stagingDir, err := g.prepareStaging()
	if err != nil {
		return err
	}
	// 失败清理 staging；换链成功后置空，交由版本目录接管
	defer func() {
		if stagingDir != "" {
			_ = os.RemoveAll(stagingDir)
		}
	}()

	if err := g.writeOutputs(stagingDir, filesToWrite); err != nil {
		return err
	}
	if err := g.verifyOutputs(stagingDir, filesToWrite); err != nil {
		return err
	}
	if err := g.deployStaging(stagingDir, len(filesToWrite)); err != nil {
		return err
	}
	stagingDir = ""
	return nil
}

func main() {
	outDir := flag.String("out", ".", "Output directory for generated rule files")
	tempParent := flag.String("temp-parent", "", "Parent directory for staging temporary files (must be on same filesystem as out)")
	timeout := flag.Duration("timeout", defaultTimeout, "Global pipeline execution timeout")
	directDomains := flag.String("direct-domains", "", "Comma-separated list of direct domains")
	directDomainsFile := flag.String("direct-domains-file", "", "Path to file containing direct domains, one per line")
	proxyDomains := flag.String("proxy-domains", "", "Comma-separated list of proxy domains")
	proxyDomainsFile := flag.String("proxy-domains-file", "", "Path to file containing proxy domains, one per line")
	cleanIpDomains := flag.String("clean-ip-domains", "", "Comma-separated list of clean-ip domains")
	cleanIpDomainsFile := flag.String("clean-ip-domains-file", "", "Path to file containing clean-ip domains, one per line")
	tmdbDomains := flag.String("tmdb-domains", "", "Comma-separated TMDB domains for DNS static mapping (empty disables tmdb.rsc)")
	tmdbDomainsFile := flag.String("tmdb-domains-file", "", "Path to file containing TMDB domains, one per line")
	tmdbECS := flag.String("tmdb-edns-subnet", defaultTmdbECS, "EDNS Client Subnet hint for DoH queries (empty disables ECS)")
	dnsmasqForwardsFile := flag.String("dnsmasq-forwards-file", "", "Path to a JSON array of {name,target,port} dnsmasq upstreams; empty disables dnsmasq outputs")
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
	cleanIp, err := loadDomainList(*cleanIpDomains, *cleanIpDomainsFile, *debug, "clean-ip")
	if err != nil {
		log.Fatalf("Load clean-ip domains failed: %v", err)
	}

	tmdb, err := loadDomainList(*tmdbDomains, *tmdbDomainsFile, *debug, "tmdb")
	if err != nil {
		log.Fatalf("Load tmdb domains failed: %v", err)
	}

	sources, err := loadSources(*sourcesPath)
	if err != nil {
		log.Fatalf("Load sources failed: %v", err)
	}
	forwards, err := loadDnsmasqForwards(*dnsmasqForwardsFile)
	if err != nil {
		log.Fatalf("Load dnsmasq forwards failed: %v", err)
	}

	gen, err := NewGenerator(*outDir, *tempParent, direct, proxy, cleanIp, tmdb, *tmdbECS, forwards, sources, *debug)
	if err != nil {
		log.Fatalf("Init generator failed: %v", err)
	}

	if err := gen.Run(ctx); err != nil {
		log.Fatalf("Generation failed: %v", err)
	}
}
