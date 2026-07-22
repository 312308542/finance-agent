package main

import (
	"testing"
	"time"

	"github.com/bensema/gotdx/types"
)

func TestParseSymbolMapsAshareMarkets(t *testing.T) {
	tests := []struct {
		input  string
		market uint8
		code   string
	}{
		{input: "600519.SH", market: types.MarketSH.Uint8(), code: "600519"},
		{input: "sz000001", market: types.MarketSZ.Uint8(), code: "000001"},
		{input: "830799.BJ", market: types.MarketBJ.Uint8(), code: "830799"},
	}
	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			market, code, err := parseSymbol(tt.input)
			if err != nil {
				t.Fatalf("parseSymbol() error = %v", err)
			}
			if market != tt.market || code != tt.code {
				t.Fatalf("parseSymbol() = market %d code %q, want market %d code %q", market, code, tt.market, tt.code)
			}
		})
	}
}

func TestParseSymbolRejectsUnsupportedMarket(t *testing.T) {
	if _, _, err := parseSymbol("00700.HK"); err == nil {
		t.Fatal("parseSymbol() should reject non-A-share symbols")
	}
}

func TestParseSymbolRejectsMissingCode(t *testing.T) {
	if _, _, err := parseSymbol("SH"); err == nil {
		t.Fatal("parseSymbol() should reject a market without a code")
	}
}

func TestClassifyFreshnessMarksWeekendAsAfterHours(t *testing.T) {
	now := time.Date(2026, time.July, 19, 21, 43, 0, 0, shanghai)
	quality, freshness, _, err := classifyFreshness(now, "15:33:10.194")
	if err != nil {
		t.Fatalf("classifyFreshness() error = %v", err)
	}
	if quality != "after_hours_snapshot" {
		t.Fatalf("quality = %q, want after_hours_snapshot", quality)
	}
	if freshness <= 0 {
		t.Fatalf("freshness = %d, want positive", freshness)
	}
}

func TestClassifyFreshnessAcceptsRecentTradingSnapshot(t *testing.T) {
	now := time.Date(2026, time.July, 20, 10, 0, 1, 0, shanghai)
	quality, freshness, _, err := classifyFreshness(now, "10:00:00.194")
	if err != nil {
		t.Fatalf("classifyFreshness() error = %v", err)
	}
	if quality != "available" {
		t.Fatalf("quality = %q, want available", quality)
	}
	if freshness < 0 || freshness > 2000 {
		t.Fatalf("freshness = %d, want [0, 2000]", freshness)
	}
}

func TestClassifyFreshnessMarksLaggedTradingSnapshot(t *testing.T) {
	now := time.Date(2026, time.July, 20, 10, 0, 30, 0, shanghai)
	quality, _, _, err := classifyFreshness(now, "09:59:00")
	if err != nil {
		t.Fatalf("classifyFreshness() error = %v", err)
	}
	if quality != "stale" {
		t.Fatalf("quality = %q, want stale", quality)
	}
}

func TestClassifyFreshnessRejectsMissingServerTime(t *testing.T) {
	now := time.Date(2026, time.July, 20, 10, 0, 0, 0, shanghai)
	quality, freshness, serverAt, err := classifyFreshness(now, "")
	if err == nil {
		t.Fatal("classifyFreshness() should reject an empty server time")
	}
	if quality != "invalid_server_time" || freshness != -1 || !serverAt.IsZero() {
		t.Fatalf("classifyFreshness() = %q, %d, %v", quality, freshness, serverAt)
	}
}
