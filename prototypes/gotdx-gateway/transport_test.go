package main

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	gotdx "github.com/bensema/gotdx"
	"github.com/bensema/gotdx/proto"
)

// 使用真实 TCP 和依赖解析器，检测网关发出的协议请求及响应错配。
type quoteFixture struct {
	listener     net.Listener
	mu           sync.Mutex
	methods      []uint16
	response     func(proto.ReqHeader, []byte) []byte
	financeDelay time.Duration
	wg           sync.WaitGroup
}

func newQuoteFixture(t *testing.T, response func(proto.ReqHeader, []byte) []byte) *quoteFixture {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	f := &quoteFixture{listener: listener, response: response}
	f.wg.Add(1)
	go func() {
		defer f.wg.Done()
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			f.wg.Add(1)
			go f.serve(conn)
		}
	}()
	t.Cleanup(func() {
		_ = listener.Close()
		f.wg.Wait()
	})
	return f
}

func (f *quoteFixture) serve(conn net.Conn) {
	defer f.wg.Done()
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	for {
		var request proto.ReqHeader
		if err := binary.Read(conn, binary.LittleEndian, &request); err != nil {
			return
		}
		body := make([]byte, int(request.PkgLen1)-2)
		if _, err := io.ReadFull(conn, body); err != nil {
			return
		}
		f.mu.Lock()
		f.methods = append(f.methods, request.Method)
		f.mu.Unlock()
		var payload []byte
		switch request.Method {
		case proto.KMSG_CMD1:
			payload = make([]byte, 189)
		case proto.KMSG_SECURITYLIST:
			payload = []byte{0, 0}
		case proto.KMSG_FINANCEINFO:
			f.mu.Lock()
			delay := f.financeDelay
			f.mu.Unlock()
			time.Sleep(delay)
			payload = make([]byte, 145)
			payload[0] = 1
			copy(payload[3:9], "000001")
		default:
			payload = f.response(request, body)
		}
		if payload == nil {
			return
		}
		header := proto.RespHeader{SeqID: request.SeqID, Method: request.Method, ZipSize: uint16(len(payload)), UnZipSize: uint16(len(payload))}
		if err := binary.Write(conn, binary.LittleEndian, header); err != nil {
			return
		}
		if _, err := conn.Write(payload); err != nil {
			return
		}
	}
}

func (f *quoteFixture) client() *gotdx.Client {
	return gotdx.New(gotdx.WithTCPAddress(f.listener.Addr().String()), gotdx.WithTCPAddressPool(), gotdx.WithTimeoutSec(1))
}

func (f *quoteFixture) connectedClient(t *testing.T) *gotdx.Client {
	t.Helper()
	client := f.client()
	if _, err := client.Connect(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Disconnect() })
	return client
}

func quotePayload(stocks ...proto.Stock) []byte {
	buffer := bytes.NewBuffer([]byte{0, 0, byte(len(stocks)), 0})
	for _, stock := range stocks {
		buffer.WriteByte(stock.Market)
		buffer.WriteString(stock.Code)
		buffer.Write(make([]byte, 2))
		// 1000 的通达信变长编码，行情价格应为 10.00；其余差值及盘口字段填零。
		buffer.Write([]byte{0xa8, 0x0f})
		buffer.Write(make([]byte, 46))
	}
	return buffer.Bytes()
}

func TestFetchQuotesDoesNotFetchMetadata(t *testing.T) {
	f := newQuoteFixture(t, func(proto.ReqHeader, []byte) []byte {
		return quotePayload(proto.Stock{Market: 0, Code: "000001"})
	})
	g := &gateway{client: f.connectedClient(t), timeoutSec: 1}
	quotes, err := g.fetchQuotes([]uint8{0}, []string{"000001"})
	if err != nil || len(quotes) != 1 || quotes[0].Price != 10 {
		t.Fatalf("行情解析异常: quotes=%+v, err=%v", quotes, err)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.methods) != 2 || f.methods[1] != proto.KMSG_SECURITYQUOTES {
		t.Fatalf("纯行情请求不应附带证券清单或逐股财务查询: methods=%x", f.methods)
	}
}

func TestFetchQuotesAvoidsLateFinanceFrame(t *testing.T) {
	f := newQuoteFixture(t, func(proto.ReqHeader, []byte) []byte {
		return quotePayload(proto.Stock{Market: 0, Code: "000001"})
	})
	f.mu.Lock()
	f.financeDelay = 1200 * time.Millisecond
	f.mu.Unlock()
	g := &gateway{client: f.connectedClient(t), timeoutSec: 1}
	defer func() {
		if failure := recover(); failure != nil {
			t.Errorf("迟到财务帧被下一次行情请求解析: %v", failure)
		}
	}()
	for attempt := 0; attempt < 2; attempt++ {
		quotes, err := g.fetchQuotes([]uint8{0}, []string{"000001"})
		if err != nil || len(quotes) != 1 || quotes[0].Code != "000001" {
			t.Fatalf("行情请求 %d 受到辅助财务响应污染: %+v, %v", attempt, quotes, err)
		}
	}
}

func TestQuotesHandlerRejectsUntrustedResponses(t *testing.T) {
	tests := []struct {
		name    string
		payload []byte
	}{
		{"missing", quotePayload()},
		{"extra", quotePayload(proto.Stock{Market: 0, Code: "000001"}, proto.Stock{Market: 1, Code: "600519"})},
		{"wrong_code", quotePayload(proto.Stock{Market: 0, Code: "000002"})},
		{"wrong_market", quotePayload(proto.Stock{Market: 1, Code: "000001"})},
		{"malformed", []byte{0, 0, 1, 0}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			f := newQuoteFixture(t, func(proto.ReqHeader, []byte) []byte { return tt.payload })
			g := &gateway{client: f.connectedClient(t), newClient: f.client, timeoutSec: 1}
			defer func() {
				if failure := recover(); failure != nil {
					t.Errorf("协议解析 panic 不应逃逸 HTTP 边界: %v", failure)
				}
			}()
			response := httptest.NewRecorder()
			g.quotesHandler(response, httptest.NewRequest(http.MethodPost, "/quotes", strings.NewReader(`{"symbols":["000001.SZ"]}`)))
			if response.Code != http.StatusBadGateway || strings.Contains(response.Body.String(), `"quotes"`) {
				t.Fatalf("不可信响应不得成功输出: status=%d, body=%s", response.Code, response.Body)
			}
			if g.client != nil {
				t.Fatal("重试失败后仍保留不可信连接")
			}
		})
	}
}

func TestQuotesHandlerPreservesResponseIdentityWhenReordered(t *testing.T) {
	f := newQuoteFixture(t, func(proto.ReqHeader, []byte) []byte {
		return quotePayload(proto.Stock{Market: 1, Code: "600519"}, proto.Stock{Market: 0, Code: "000001"})
	})
	g := &gateway{client: f.connectedClient(t), timeoutSec: 1}
	response := httptest.NewRecorder()
	g.quotesHandler(response, httptest.NewRequest(http.MethodPost, "/quotes", strings.NewReader(`{"symbols":["000001.SZ","600519.SH"]}`)))
	var body struct {
		Quotes []quoteResponse `json:"quotes"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil || response.Code != http.StatusOK || len(body.Quotes) != 2 {
		t.Fatalf("合法乱序响应解析失败: status=%d, body=%s, err=%v", response.Code, response.Body, err)
	}
	for _, quote := range body.Quotes {
		if quote.Symbol != quote.Code+"."+quote.Market {
			t.Fatalf("行情身份被请求顺序覆盖: %+v", quote)
		}
	}
}

func TestWithClientDiscardsFailedRetry(t *testing.T) {
	for _, panics := range []bool{false, true} {
		t.Run(map[bool]string{false: "error", true: "panic"}[panics], func(t *testing.T) {
			f := newQuoteFixture(t, func(proto.ReqHeader, []byte) []byte { return nil })
			g := &gateway{client: f.connectedClient(t), newClient: f.client, timeoutSec: 1}
			calls := 0
			defer func() {
				if failure := recover(); failure != nil {
					t.Errorf("调用 panic 不应逃逸: %v", failure)
				}
			}()
			_, err := withClient(g, func(*gotdx.Client) (int, error) {
				calls++
				if panics {
					panic("bad upstream frame")
				}
				return 42, errors.New("bad upstream frame")
			})
			if err == nil || calls != 2 || g.client != nil {
				t.Fatalf("必须有限重试并清理最终失败连接: calls=%d, err=%v, client=%p", calls, err, g.client)
			}
		})
	}
}

func TestQuotesHandlerRejectsNonAshareInstruments(t *testing.T) {
	for _, symbol := range []string{"510300.SH", "159915.SZ", "000001.SH", "900901.SH", "6000.SH", "ABCDEF.SH"} {
		t.Run(symbol, func(t *testing.T) {
			market, code, _ := parseSymbol(symbol)
			f := newQuoteFixture(t, func(proto.ReqHeader, []byte) []byte {
				return quotePayload(proto.Stock{Market: market, Code: code})
			})
			g := &gateway{client: f.connectedClient(t), newClient: f.client, timeoutSec: 1}
			response := httptest.NewRecorder()
			g.quotesHandler(response, httptest.NewRequest(http.MethodPost, "/quotes", strings.NewReader(fmt.Sprintf(`{"symbols":[%q]}`, symbol))))
			if response.Code != http.StatusBadRequest {
				t.Fatalf("非 A 股证券不应按两位价格解释: symbol=%s, status=%d", symbol, response.Code)
			}
		})
	}
}

func TestQuotesHandlerPreservesAsharePricesAndProtocolBoundary(t *testing.T) {
	for _, count := range []int{3, 80} {
		t.Run(fmt.Sprint(count), func(t *testing.T) {
			f := newQuoteFixture(t, func(_ proto.ReqHeader, body []byte) []byte {
				stocks := make([]proto.Stock, 0)
				for offset := 10; offset < len(body); offset += 7 {
					stocks = append(stocks, proto.Stock{Market: body[offset], Code: string(body[offset+1 : offset+7])})
				}
				return quotePayload(stocks...)
			})
			g := &gateway{newClient: f.client, timeoutSec: 1}
			t.Cleanup(func() {
				if g.client != nil {
					_ = g.client.Disconnect()
				}
			})
			symbols := []string{"000001.SZ", "600000.SH", "830799.BJ"}
			if count == 80 {
				symbols = nil
				for index := 1; index <= count; index++ {
					symbols = append(symbols, fmt.Sprintf("%06d.SZ", index))
				}
			}
			payload, _ := json.Marshal(quoteRequest{Symbols: symbols})
			response := httptest.NewRecorder()
			g.quotesHandler(response, httptest.NewRequest(http.MethodPost, "/quotes", bytes.NewReader(payload)))
			var body struct {
				Quotes []quoteResponse `json:"quotes"`
			}
			if err := json.Unmarshal(response.Body.Bytes(), &body); err != nil || response.Code != http.StatusOK || len(body.Quotes) != count {
				t.Fatalf("边界请求失败: status=%d, body=%s, err=%v", response.Code, response.Body, err)
			}
			for _, quote := range body.Quotes {
				if quote.LastPrice != 10 || quote.PrevClose != 10 || quote.BidLevels[0].Price != 10 || quote.AskLevels[0].Price != 10 || quote.ServerTime != "00:00:00.000" {
					t.Fatalf("价格或服务端时间被改写: %+v", quote)
				}
			}
		})
	}
}

func TestQuotesHandlerRejectsProtocolOverflow(t *testing.T) {
	symbols := make([]string, 0, 81)
	for index := 1; index <= 81; index++ {
		symbols = append(symbols, fmt.Sprintf("%06d.SZ", index))
	}
	payload, _ := json.Marshal(quoteRequest{Symbols: symbols})
	response := httptest.NewRecorder()
	(&gateway{timeoutSec: 1}).quotesHandler(
		response,
		httptest.NewRequest(http.MethodPost, "/quotes", bytes.NewReader(payload)),
	)
	if response.Code != http.StatusBadRequest || !strings.Contains(response.Body.String(), "1 到 80") {
		t.Fatalf("超过协议上限的请求应在连接前拒绝: status=%d, body=%s", response.Code, response.Body)
	}
}

func TestFetchQuotesReconnectsAfterUntrustedResponse(t *testing.T) {
	var responses atomic.Int32
	f := newQuoteFixture(t, func(proto.ReqHeader, []byte) []byte {
		if responses.Add(1) == 1 {
			return quotePayload(proto.Stock{Market: 1, Code: "600000"})
		}
		return quotePayload(proto.Stock{Market: 0, Code: "000001"})
	})
	g := &gateway{newClient: f.client, timeoutSec: 1}
	t.Cleanup(func() {
		if g.client != nil {
			_ = g.client.Disconnect()
		}
	})
	quotes, err := g.fetchQuotes([]uint8{0}, []string{"000001"})
	if err != nil || len(quotes) != 1 || quotes[0].Code != "000001" || responses.Load() != 2 {
		t.Fatalf("错配后未在新连接上恢复: quotes=%+v, err=%v", quotes, err)
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.methods) != 4 || f.methods[0] != proto.KMSG_CMD1 || f.methods[2] != proto.KMSG_CMD1 {
		t.Fatalf("重试必须重新握手: methods=%x", f.methods)
	}
}

func TestQuotesHandlerRejectsDuplicateResponse(t *testing.T) {
	f := newQuoteFixture(t, func(proto.ReqHeader, []byte) []byte {
		return quotePayload(proto.Stock{Market: 0, Code: "000001"}, proto.Stock{Market: 0, Code: "000001"})
	})
	g := &gateway{newClient: f.client, timeoutSec: 1}
	response := httptest.NewRecorder()
	g.quotesHandler(response, httptest.NewRequest(http.MethodPost, "/quotes", strings.NewReader(`{"symbols":["000001.SZ","600000.SH"]}`)))
	if response.Code != http.StatusBadGateway || g.client != nil {
		t.Fatalf("重复响应未被拒绝: status=%d", response.Code)
	}
}
