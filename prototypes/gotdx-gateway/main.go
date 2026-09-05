package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	gotdx "github.com/bensema/gotdx"
	"github.com/bensema/gotdx/proto"
	"github.com/bensema/gotdx/types"
)

var shanghai = time.FixedZone("Asia/Shanghai", 8*60*60)

const (
	defaultListenAddr = "127.0.0.1:8790"
	defaultTimeoutSec = 3
	maxQuoteSymbols   = 100
	staleAfter        = 10 * time.Second
)

type gateway struct {
	mu         sync.Mutex
	client     *gotdx.Client
	newClient  func() *gotdx.Client
	timeoutSec int
}

type quoteRequest struct {
	Symbols []string `json:"symbols"`
}

type unusualRequest struct {
	Market string `json:"market"`
	Start  uint32 `json:"start"`
	Count  uint32 `json:"count"`
}

type quoteResponse struct {
	Source            string       `json:"source"`
	Symbol            string       `json:"symbol"`
	Market            string       `json:"market"`
	Code              string       `json:"code"`
	LastPrice         float64      `json:"last_price"`
	PrevClose         float64      `json:"prev_close"`
	Open              float64      `json:"open"`
	High              float64      `json:"high"`
	Low               float64      `json:"low"`
	Volume            int          `json:"volume"`
	Amount            float64      `json:"amount"`
	ServerTime        string       `json:"server_time"`
	ServerTimestamp   string       `json:"server_timestamp,omitempty"`
	ReceivedAt        time.Time    `json:"received_at"`
	FreshnessMs       int64        `json:"freshness_ms"`
	QualityStatus     string       `json:"quality_status"`
	ProviderLatencyMs int64        `json:"provider_latency_ms"`
	BidLevels         []quoteLevel `json:"bid_levels"`
	AskLevels         []quoteLevel `json:"ask_levels"`
}

type quoteLevel struct {
	Price  float64 `json:"price"`
	Volume int     `json:"volume"`
}

func main() {
	listenAddr := envString("TDX_GATEWAY_ADDR", defaultListenAddr)
	timeoutSec := envInt("TDX_TIMEOUT_SECONDS", defaultTimeoutSec)
	if timeoutSec < 1 {
		timeoutSec = defaultTimeoutSec
	}
	g := &gateway{timeoutSec: timeoutSec}

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", g.healthHandler)
	mux.HandleFunc("/quotes", g.quotesHandler)
	mux.HandleFunc("/unusual", g.unusualHandler)
	server := &http.Server{
		Addr:              listenAddr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		// 首次 gotdx 请求可能包含节点测速，不能用普通 HTTP 接口的 10 秒写超时。
		WriteTimeout: 30 * time.Second,
		IdleTimeout:  30 * time.Second,
	}
	log.Printf("gotdx gateway listening on %s", listenAddr)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}

func (g *gateway) healthHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "只支持 GET")
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"status":      "ok",
		"source":      "gotdx:tdx_main",
		"received_at": time.Now().In(shanghai),
	})
}

func (g *gateway) quotesHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "只支持 POST")
		return
	}
	var request quoteRequest
	if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是有效 JSON")
		return
	}
	if len(request.Symbols) == 0 || len(request.Symbols) > maxQuoteSymbols {
		writeError(w, http.StatusBadRequest, fmt.Sprintf("symbols 数量必须在 1 到 %d 之间", maxQuoteSymbols))
		return
	}
	markets := make([]uint8, 0, len(request.Symbols))
	codes := make([]string, 0, len(request.Symbols))
	seen := make(map[string]struct{}, len(request.Symbols))
	for _, symbol := range request.Symbols {
		market, code, err := parseSymbol(symbol)
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		if !isAshareStock(market, code) {
			writeError(w, http.StatusBadRequest, "仅支持六位代码的沪深北 A 股股票，不支持基金、指数或债券")
			return
		}
		markets = append(markets, market)
		codes = append(codes, code)
		canonical := canonicalSymbol(market, code)
		if _, exists := seen[canonical]; exists {
			writeError(w, http.StatusBadRequest, "symbols 不能包含重复证券")
			return
		}
		seen[canonical] = struct{}{}
	}

	started := time.Now()
	quotes, err := g.fetchQuotes(markets, codes)
	latency := time.Since(started).Milliseconds()
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	receivedAt := time.Now().In(shanghai)
	items := make([]quoteResponse, 0, len(quotes))
	for _, quote := range quotes {
		market := quoteMarket(quote.Market)
		code := quote.Code
		symbol := canonicalSymbol(quote.Market, code)
		quality, freshness, serverAt, _ := classifyFreshness(receivedAt, quote.ServerTime)
		item := quoteResponse{
			Source:            "gotdx:tdx_main",
			Symbol:            symbol,
			Market:            market,
			Code:              code,
			LastPrice:         quote.Price,
			PrevClose:         quote.PreClose,
			Open:              quote.Open,
			High:              quote.High,
			Low:               quote.Low,
			Volume:            quote.Vol,
			Amount:            quote.Amount,
			ServerTime:        quote.ServerTime,
			ReceivedAt:        receivedAt,
			FreshnessMs:       freshness,
			QualityStatus:     quality,
			ProviderLatencyMs: latency,
			BidLevels:         levels(quote.BidLevels),
			AskLevels:         levels(quote.AskLevels),
		}
		if !serverAt.IsZero() {
			item.ServerTimestamp = serverAt.Format(time.RFC3339Nano)
		}
		items = append(items, item)
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"source":       "gotdx:tdx_main",
		"received_at":  receivedAt,
		"latency_ms":   latency,
		"quotes":       items,
		"quality_gate": "收盘后和非交易日快照不得直接触发卖出信号",
	})
}

func (g *gateway) unusualHandler(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "只支持 POST")
		return
	}
	var request unusualRequest
	if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
		writeError(w, http.StatusBadRequest, "请求体不是有效 JSON")
		return
	}
	market, err := parseMarket(request.Market)
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	if request.Count == 0 {
		request.Count = 100
	}
	if request.Count > 600 {
		writeError(w, http.StatusBadRequest, "count 不能超过 600")
		return
	}
	started := time.Now()
	items, err := g.fetchUnusual(market, request.Start, request.Count)
	if err != nil {
		writeError(w, http.StatusBadGateway, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"source":      "gotdx:tdx_main",
		"received_at": time.Now().In(shanghai),
		"latency_ms":  time.Since(started).Milliseconds(),
		"market":      quoteMarket(market),
		"items":       items,
	})
}

func (g *gateway) fetchQuotes(markets []uint8, codes []string) ([]proto.SecurityQuote, error) {
	return withClient(g, func(client *gotdx.Client) ([]proto.SecurityQuote, error) {
		// A 股价格按分解析；不调用会吞掉辅助财务请求超时的高层补全接口。
		reply, err := client.GetQuotesDetail(markets, codes)
		if err != nil {
			return nil, err
		}
		if err := validateQuotes(markets, codes, reply.List); err != nil {
			return nil, err
		}
		return reply.List, nil
	})
}

func validateQuotes(markets []uint8, codes []string, quotes []proto.SecurityQuote) error {
	if len(markets) != len(codes) || len(quotes) != len(codes) {
		return fmt.Errorf("行情数量不匹配: 请求 %d，收到 %d", len(codes), len(quotes))
	}
	expected := make(map[proto.Stock]struct{}, len(codes))
	for index, code := range codes {
		expected[proto.Stock{Market: markets[index], Code: code}] = struct{}{}
	}
	for _, quote := range quotes {
		key := proto.Stock{Market: quote.Market, Code: quote.Code}
		if _, ok := expected[key]; !ok {
			return fmt.Errorf("行情包含非请求或重复证券: %s", canonicalSymbol(quote.Market, quote.Code))
		}
		delete(expected, key)
	}
	if len(expected) != 0 {
		return errors.New("行情缺少请求证券")
	}
	return nil
}

func isAshareStock(market uint8, code string) bool {
	if len(code) != 6 {
		return false
	}
	for _, digit := range code {
		if digit < '0' || digit > '9' {
			return false
		}
	}
	switch market {
	case types.MarketSH.Uint8():
		return strings.HasPrefix(code, "60") || strings.HasPrefix(code, "68")
	case types.MarketSZ.Uint8():
		return strings.HasPrefix(code, "00") || strings.HasPrefix(code, "30")
	case types.MarketBJ.Uint8():
		return code[0] == '4' || code[0] == '8' || strings.HasPrefix(code, "92")
	default:
		return false
	}
}

func (g *gateway) fetchUnusual(market uint8, start, count uint32) ([]proto.UnusualData, error) {
	return withClient(g, func(client *gotdx.Client) ([]proto.UnusualData, error) {
		return client.StockUnusual(market, start, count)
	})
}

func withClient[T any](g *gateway, call func(*gotdx.Client) (T, error)) (T, error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	var zero T
	var lastErr error
	for attempt := 0; attempt < 2; attempt++ {
		result, err := func() (result T, err error) {
			// 第三方解析器直接切片，异常帧必须转失败并使连接失效，不能返回部分结果。
			defer func() {
				if failure := recover(); failure != nil {
					result = zero
					err = fmt.Errorf("上游协议解析异常: %v", failure)
				}
			}()
			if g.client == nil {
				if g.newClient != nil {
					g.client = g.newClient()
				} else {
					g.client = gotdx.New(gotdx.WithAutoSelectFastest(true), gotdx.WithTimeoutSec(g.timeoutSec))
				}
				if _, err := g.client.Connect(); err != nil {
					return zero, err
				}
			}
			return call(g.client)
		}()
		if err == nil {
			return result, nil
		}
		lastErr = err
		// 初次和重试失败都丢弃连接，禁止迟到响应被后续请求消费。
		if g.client != nil {
			_ = g.client.Disconnect()
			g.client = nil
		}
	}
	return zero, lastErr
}

func parseSymbol(value string) (uint8, string, error) {
	value = strings.ToUpper(strings.TrimSpace(value))
	if value == "" {
		return 0, "", errors.New("symbol 不能为空")
	}
	if strings.Contains(value, ".") {
		parts := strings.Split(value, ".")
		if len(parts) != 2 || parts[0] == "" {
			return 0, "", fmt.Errorf("symbol 格式无效: %s", value)
		}
		market, err := parseMarket(parts[1])
		if err != nil {
			return 0, "", err
		}
		if strings.TrimSpace(parts[0]) == "" {
			return 0, "", fmt.Errorf("symbol 缺少证券代码: %s", value)
		}
		return market, parts[0], nil
	}
	if strings.HasPrefix(value, "SH") || strings.HasPrefix(value, "SZ") || strings.HasPrefix(value, "BJ") {
		market, err := parseMarket(value[:2])
		if err != nil {
			return 0, "", err
		}
		if value[2:] == "" {
			return 0, "", fmt.Errorf("symbol 缺少证券代码: %s", value)
		}
		return market, value[2:], nil
	}
	if len(value) == 6 {
		switch value[0] {
		case '6':
			return types.MarketSH.Uint8(), value, nil
		case '0', '3':
			return types.MarketSZ.Uint8(), value, nil
		case '8', '4':
			return types.MarketBJ.Uint8(), value, nil
		}
	}
	return 0, "", fmt.Errorf("仅支持 SH/SZ/BJ A 股代码: %s", value)
}

func parseMarket(value string) (uint8, error) {
	switch strings.ToUpper(strings.TrimSpace(value)) {
	case "SH", "SSE", "1":
		return types.MarketSH.Uint8(), nil
	case "SZ", "SZSE", "0":
		return types.MarketSZ.Uint8(), nil
	case "BJ", "BSE", "2":
		return types.MarketBJ.Uint8(), nil
	default:
		return 0, fmt.Errorf("仅支持 SH/SZ/BJ 市场: %s", value)
	}
}

func canonicalSymbol(market uint8, code string) string {
	return code + "." + quoteMarket(market)
}

func quoteMarket(market uint8) string {
	switch market {
	case types.MarketSH.Uint8():
		return "SH"
	case types.MarketSZ.Uint8():
		return "SZ"
	case types.MarketBJ.Uint8():
		return "BJ"
	default:
		return "UNKNOWN"
	}
}

func classifyFreshness(now time.Time, serverTime string) (string, int64, time.Time, error) {
	serverAt, err := parseServerTime(now, serverTime)
	if err != nil {
		return "invalid_server_time", -1, time.Time{}, err
	}
	freshness := now.Sub(serverAt).Milliseconds()
	if !isTradingTime(now) {
		return "after_hours_snapshot", freshness, serverAt, nil
	}
	if freshness < -5_000 {
		return "clock_skew", freshness, serverAt, nil
	}
	if freshness > staleAfter.Milliseconds() {
		return "stale", freshness, serverAt, nil
	}
	return "available", freshness, serverAt, nil
}

func parseServerTime(now time.Time, value string) (time.Time, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return time.Time{}, errors.New("服务端时间为空")
	}
	var parsed time.Time
	var err error
	for _, layout := range []string{"15:04:05.999999999", "15:04:05"} {
		parsed, err = time.ParseInLocation(layout, value, shanghai)
		if err == nil {
			return time.Date(now.In(shanghai).Year(), now.In(shanghai).Month(), now.In(shanghai).Day(), parsed.Hour(), parsed.Minute(), parsed.Second(), parsed.Nanosecond(), shanghai), nil
		}
	}
	return time.Time{}, fmt.Errorf("无法解析服务端时间 %q", value)
}

func isTradingTime(value time.Time) bool {
	value = value.In(shanghai)
	if value.Weekday() == time.Saturday || value.Weekday() == time.Sunday {
		return false
	}
	minutes := value.Hour()*60 + value.Minute()
	return minutes >= 9*60+30 && minutes <= 11*60+30 || minutes >= 13*60 && minutes <= 15*60
}

func levels(items []proto.Level) []quoteLevel {
	result := make([]quoteLevel, 0, len(items))
	for _, item := range items {
		result = append(result, quoteLevel{Price: item.Price, Volume: item.Vol})
	}
	return result
}

func envString(name, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(name)); value != "" {
		return value
	}
	return fallback
}

func envInt(name string, fallback int) int {
	value, err := strconv.Atoi(strings.TrimSpace(os.Getenv(name)))
	if err != nil {
		return fallback
	}
	return value
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}
