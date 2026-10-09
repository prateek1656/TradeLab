"""Index ETF catalogue for the Holding section: the classifier, the built-in list, search and the live Kotak merge."""

import pytest

from app.services import etfs
from app.services.etf_data import CORE, INDEX_ETFS, NON_INDEX_SYMBOLS
from app.services.scrips import Instrument

CORE_SYMBOLS = [
    "NIFTYBEES", "ITBEES", "BANKBEES", "JUNIORBEES", "PSUBNKBEES", "SETFNIF50", "NIFTYIETF", "MID150BEES", "CPSEETF",
    "PHARMABEES", "NIFTYETF", "AUTOBEES", "NIFTY1", "MIDCAPETF", "NEXT50IETF", "PSUBANK", "HDFCNIFTY", "SETFNIFBK",
    "IT", "SETFNN50", "HDFCNIFBAN", "NEXT50", "ITETF",
]

# (symbol, text as NSE / Kotak describe it) - none of these may ever be offered.
NOT_INDEX = [
    ("GOLDBEES", "Gold"),
    ("SILVERBEES", "NipponIndiaSilverETF"),
    ("LIQUIDBEES", "GovernmentSecurities"),
    ("LIQUIDCASE", "ZerodhaNifty1DRateLiquidETF"),
    ("BBETF0432", "NiftyBHARATBondIndex-April2032"),
    ("EBBETF0433", "BHARATBondETF-April2033"),
    ("GILT5YBEES", "Nifty5yrBenchmarkG-SecIndex"),
    ("SETF10GILT", "Nifty10yrBenchmarkG-SecIndex"),
    ("LICNETFGSC", "GSEC10NSEIndex"),
    ("AXISBPSETF", "NiftyAAABondPlusSDLApr202650:50Index"),
    ("HYBRIDETF", "MIRAEAMC - HYBRIDETF"),
    ("GSEC10ABSL", "CRISIL 10 Year Gilt ETF"),
    ("ABGSEC", "Aditya Birla Sun Life CRISIL Broad Based Gilt ETF"),
    ("ABSLLIQUID", "AdityaBirlaSunLifeCRISILOvernightFundAIIndexETF"),
    ("CASHIETF", "BSE Liquid Rate Index"),
    ("MOGOLD", "Domestic Price of Physical Gold"),
    ("GROWWGOLD", "Commodity"),
]


class FakeScrips:
    """Stands in for scrip_master: just the state etfs.py reads."""

    def __init__(self, instruments, source="kotak", loaded_on="2026-10-09"):
        self.instruments = instruments
        self.source = source
        self.loaded_on = loaded_on
        self._by_ts = {i.trading_symbol: i for i in instruments}

    def token_for(self, trading_symbol):
        inst = self._by_ts.get(trading_symbol)
        return inst.token if inst else None


@pytest.fixture(autouse=True)
def fresh_catalogue(monkeypatch):
    monkeypatch.setattr(etfs, "etf_catalogue", etfs.EtfCatalogue())


@pytest.fixture
def builtin(monkeypatch):
    """No Kotak list loaded yet: the built-in list is shown."""
    monkeypatch.setattr(etfs, "scrip_master", FakeScrips([], source="builtin", loaded_on=None))


def _live_rows(skip=()):
    """A Kotak-style row for every built-in ETF (tokens 10000, 10001, ...), minus `skip`."""
    return [
        Instrument(symbol=s, trading_symbol=f"{s}-EQ", name=f"SOMEAMC - {s}", token=str(10000 + n), isin=isin)
        for n, (s, _name, isin, _group) in enumerate(INDEX_ETFS) if s not in skip
    ]


def _symbols(rows):
    return [e.symbol for e in rows]


# ---- the rule ---------------------------------------------------------------------------------------------------

def test_every_core_symbol_is_an_index_etf(builtin):
    rows = {s: (name, isin) for s, name, isin, _ in INDEX_ETFS}
    assert list(CORE) == CORE_SYMBOLS
    for s in CORE_SYMBOLS:
        name, isin = rows[s]
        assert etfs.looks_like_index_etf(s, name, isin), s
        assert etfs.looks_like_index_etf(s), s
        assert etfs.is_index_etf(s), s
        assert etfs.find(s).core is True


@pytest.mark.parametrize("symbol,text", NOT_INDEX)
def test_gold_silver_liquid_and_debt_etfs_are_not_index_etfs(builtin, symbol, text):
    assert not etfs.looks_like_index_etf(symbol, text)
    assert not etfs.is_index_etf(symbol)
    assert symbol in NON_INDEX_SYMBOLS


@pytest.mark.parametrize("symbol,text", [
    ("COMMOIETF", "ICICIPrudentialNiftyCommoditiesETF"),
    ("CPSEETF", "Goldman Sachs CPSE ETF"),
    ("ICICIB22", "S&PBSEBHARAT22index"),
    ("MIDSELIETF", "NiftyGS813Yr"),
    ("ESG", "Nifty100ESGSectorLeadersTotalReturnIndex"),
])
def test_equity_funds_with_misleading_names_are_index_etfs(builtin, symbol, text):
    assert etfs.looks_like_index_etf(symbol, text, "INF109KC19O8")
    assert etfs.is_index_etf(symbol)


def test_rule_on_names_it_has_never_seen():
    assert not etfs.looks_like_index_etf("NEWDEBT", "XYZ CRISIL IBX AAA Index Jun 2027")
    assert not etfs.looks_like_index_etf("NEWSDL", "Nifty SDL Plus G-Sec Jun 2028")
    assert not etfs.looks_like_index_etf("TBILLETF", "Nifty 1Y T-Bill")
    assert not etfs.looks_like_index_etf("NEWARB", "Some Arbitrage ETF")
    assert not etfs.looks_like_index_etf("BBETF0440")
    assert etfs.looks_like_index_etf("NEWDEF", "Nifty India Defence")
    assert etfs.looks_like_index_etf("HOUSING", "Nifty Housing Sector")  # "g Sec-tor" is not G-Sec
    assert etfs.looks_like_index_etf("CAPMKT", "Nifty Capital Markets")


def test_rule_checks_the_isin_only_when_given():
    assert etfs.looks_like_index_etf("NIFTYBEES", "Nifty 50", "INF204KB14I2")
    assert etfs.looks_like_index_etf("NIFTYBEES", "Nifty 50")
    assert not etfs.looks_like_index_etf("NIFTYBEES", "Nifty 50", "INE002A01018")
    assert not etfs.looks_like_index_etf("RELIANCE", "Reliance Industries", "")


# ---- the built-in list ------------------------------------------------------------------------------------------

def test_builtin_catalogue_is_complete_and_clean(builtin):
    cat = etfs.catalogue()
    assert 200 <= len(cat) <= 330
    syms = _symbols(cat)
    assert len(syms) == len(set(syms))
    assert len({e.isin for e in cat}) == len(cat)
    for e in cat:
        assert e.trading_symbol == f"{e.symbol}-EQ"
        assert e.isin.startswith("INF")
        assert e.group in etfs.GROUPS
        assert e.name and "\t" not in e.name and "  " not in e.name and e.name == e.name.strip()
        assert e.exchange_segment == "nse_cm" and e.token is None
        assert etfs.looks_like_index_etf(e.symbol, e.name, e.isin), e.symbol
        assert e.symbol not in NON_INDEX_SYMBOLS
    # every group has something in it
    assert {e.group for e in cat} == set(etfs.GROUPS)


def test_readable_names_and_groups(builtin):
    assert etfs.find("NIFTYBEES").name == "Nifty 50"
    assert etfs.find("BANKBEES").name == "Nifty Bank"
    assert etfs.find("BANKBEES").group == "Bank & Financials"
    assert etfs.find("JUNIORBEES").group == "Nifty Next 50"
    assert etfs.find("MON100").group == "International"
    # the NSE file swaps these two; the built-in list has them the right way round
    assert etfs.find("SETFNIFBK").name == "Nifty Bank"
    assert etfs.find("SETFNN50").name == "Nifty Next 50"


def test_catalogue_order_core_first_then_group_then_symbol(builtin):
    cat = etfs.catalogue()
    assert _symbols(cat[:len(CORE_SYMBOLS)]) == CORE_SYMBOLS
    rest = cat[len(CORE_SYMBOLS):]
    assert not any(e.core for e in rest)
    keys = [(etfs.GROUPS.index(e.group), e.symbol) for e in rest]
    assert keys == sorted(keys)


def test_groups_in_display_order():
    assert etfs.groups() == ["Broad market", "Nifty Next 50", "Midcap & Smallcap", "Bank & Financials", "Sensex",
                             "Sector & Theme", "Factor & Strategy", "International"]


def test_to_dict(builtin):
    d = etfs.find("NIFTYBEES").to_dict()
    assert d == {"symbol": "NIFTYBEES", "trading_symbol": "NIFTYBEES-EQ", "name": "Nifty 50", "isin": "INF204KB14I2",
                 "group": "Broad market", "core": True, "token": None, "exchange_segment": "nse_cm"}


# ---- is_index_etf / search --------------------------------------------------------------------------------------

def test_is_index_etf_accepts_any_spelling_of_the_symbol(builtin):
    assert etfs.is_index_etf("NIFTYBEES-EQ")
    assert etfs.is_index_etf("niftybees")
    assert etfs.is_index_etf(" NiftyBees ", "Nifty 50")
    assert not etfs.is_index_etf("RELIANCE")
    assert not etfs.is_index_etf("GOLDBEES-EQ")
    assert not etfs.is_index_etf("")


def test_search_bank_puts_bankbees_first(builtin):
    found = _symbols(etfs.search("bank"))
    assert found[0] == "BANKBEES"
    assert "PSUBNKBEES" in found and "HDFCNIFBAN" in found


def test_search_empty_starts_with_the_core_list(builtin):
    found = etfs.search("")
    assert found[0].symbol == "NIFTYBEES"
    assert _symbols(found[:len(CORE_SYMBOLS)]) == CORE_SYMBOLS
    assert len(found) == 60
    assert len(etfs.search("", limit=5)) == 5


def test_search_scoring(builtin):
    assert etfs.search("NIFTYBEES-EQ")[0].symbol == "NIFTYBEES"
    assert etfs.search("it")[0].symbol == "IT"
    assert etfs.search("itbees")[0].symbol == "ITBEES"
    top = etfs.search("nifty bank")[:5]
    assert top[0].symbol == "BANKBEES" and all(e.name == "Nifty Bank" for e in top)
    assert "MODEFENCE" in _symbols(etfs.search("defence"))
    assert "SETFNIF50" in _symbols(etfs.search("nifty50"))
    assert etfs.search("international")[0].group == "International"
    assert etfs.search("gold") == [] and etfs.search("liquid") == []
    assert len(etfs.search("nifty", limit=7)) == 7


# ---- live Kotak list ----------------------------------------------------------------------------------------------

def test_live_list_merge(monkeypatch):
    gone = {"HDFCNIMEG", "MOMMIDCAP", "NIFTYAXIS"}
    axis_isin = next(isin for s, _n, isin, _g in INDEX_ETFS if s == "NIFTYAXIS")
    rows = _live_rows(skip=gone) + [
        # a new index ETF that only Kotak knows about
        Instrument(symbol="DEFENCENEW", trading_symbol="DEFENCENEW-EQ", name="XYZAMC - DEFENCENEW", token="777",
                   isin="INF999X01011"),
        # a company share, a new gold ETF and a known liquid ETF: never offered
        Instrument(symbol="RELIANCE", trading_symbol="RELIANCE-EQ", name="RELIANCE INDUSTRIES LTD", token="2885",
                   isin="INE002A01018"),
        Instrument(symbol="NEWGLDETF", trading_symbol="NEWGLDETF-EQ", name="XYZAMC - GOLD ETF", token="778",
                   isin="INF999X01029"),
        Instrument(symbol="LIQUIDBEES", trading_symbol="LIQUIDBEES-EQ", name="NIPINDETFLIQUIDBEES", token="779",
                   isin="INF732E01037"),
        Instrument(symbol="CASHETF2", trading_symbol="CASHETF2-EQ", name="XYZAMC - CASHETF2", token="780",
                   isin="INF999X01037"),
        # the same fund as NIFTYAXIS under its old symbol: keeps the readable name
        Instrument(symbol="AXISNIFTY", trading_symbol="AXISNIFTY-EQ", name="AXISAMC-AXISNIFTY", token="781",
                   isin=axis_isin),
        # block-deal series rows are not tradable for us
        Instrument(symbol="BLOCKETF", trading_symbol="BLOCKETF-BL", name="XYZAMC - BLOCKETF", token="782",
                   isin="INF999X01045", series="BL"),
    ]
    monkeypatch.setattr(etfs, "scrip_master", FakeScrips(rows))

    cat = etfs.catalogue()
    by = {e.symbol: e for e in cat}
    # built-in rows missing from the live list are dropped
    assert not gone & set(by)
    assert len(cat) == len(INDEX_ETFS) - len(gone) + 2
    # the new ETF is added with Kotak's name and a guessed group
    new = by["DEFENCENEW"]
    assert new.token == "777" and new.name == "XYZAMC - DEFENCENEW" and new.group == "Sector & Theme"
    assert new.trading_symbol == "DEFENCENEW-EQ" and new.isin == "INF999X01011" and not new.core
    assert etfs.is_index_etf("defencenew-eq")
    # never offered
    for s in ("RELIANCE", "NEWGLDETF", "LIQUIDBEES", "CASHETF2", "BLOCKETF"):
        assert s not in by and not etfs.is_index_etf(s)
    # renamed fund found by ISIN
    assert by["AXISNIFTY"].name == "Nifty 50" and by["AXISNIFTY"].token == "781"
    # tokens are filled from the live list
    tokens = {s: str(10000 + n) for n, (s, *_rest) in enumerate(INDEX_ETFS)}
    assert by["NIFTYBEES"].token == tokens["NIFTYBEES"]
    assert all(e.token for e in cat)
    # still core first, in order
    assert _symbols(cat[:len(CORE_SYMBOLS)]) == CORE_SYMBOLS


def test_builtin_rows_shown_without_tokens_until_live_list_loads(builtin):
    cat = etfs.catalogue()
    assert len(cat) == len(INDEX_ETFS)
    assert all(e.token is None for e in cat)


def test_broken_live_list_falls_back_to_builtin(monkeypatch):
    rows = _live_rows()[:5]
    monkeypatch.setattr(etfs, "scrip_master", FakeScrips(rows))
    cat = etfs.catalogue()
    assert len(cat) == len(INDEX_ETFS)
    assert sum(1 for e in cat if e.token) == 5


def test_guess_group():
    assert etfs.guess_group("NEWBANKETF", "XYZAMC - NEWBANKETF") == "Bank & Financials"
    assert etfs.guess_group("NEWSENSEX", "") == "Sensex"
    assert etfs.guess_group("NASDAQETF", "") == "International"
    assert etfs.guess_group("SMALLNEW", "") == "Midcap & Smallcap"
    assert etfs.guess_group("NEWETF", "XYZAMC - NEWETF") == "Broad market"


def test_catalogue_is_cached_until_the_scrip_list_changes(monkeypatch):
    calls = []
    real = etfs._merge

    def counting(sm, instruments):
        calls.append(1)
        return real(sm, instruments)

    monkeypatch.setattr(etfs, "_merge", counting)
    fake = FakeScrips(_live_rows())
    monkeypatch.setattr(etfs, "scrip_master", fake)
    first = etfs.catalogue()
    etfs.search("bank")
    etfs.is_index_etf("NIFTYBEES")
    assert etfs.catalogue() == first
    assert len(calls) == 1
    # a new day's download
    fake.instruments = _live_rows(skip={"NIFTYBEES"})
    fake.loaded_on = "2026-10-10"
    assert not etfs.is_index_etf("NIFTYBEES")
    assert len(calls) == 2
    # callers can't damage the cache
    etfs.catalogue().clear()
    assert etfs.catalogue()
