"""ETF catalogue for the Holding section: the index rule, the built-in list (index ETFs, then bond, gold, silver,
liquid and other ETFs), search and the live Kotak merge."""

import re

import pytest

from app.services import etfs
from app.services.etf_data import CORE, INDEX_ETFS, NON_INDEX_SYMBOLS, OTHER_ETFS
from app.services.scrips import Instrument

CORE_SYMBOLS = [
    "NIFTYBEES", "ITBEES", "BANKBEES", "JUNIORBEES", "PSUBNKBEES", "SETFNIF50", "NIFTYIETF", "MID150BEES", "CPSEETF",
    "PHARMABEES", "NIFTYETF", "AUTOBEES", "NIFTY1", "MIDCAPETF", "NEXT50IETF", "PSUBANK", "HDFCNIFTY", "SETFNIFBK",
    "IT", "SETFNN50", "HDFCNIFBAN", "NEXT50", "ITETF",
]

# Every built-in ETF: (symbol, name, ISIN).
ALL_ROWS = [(s, name, isin) for s, name, isin, _ in INDEX_ETFS + OTHER_ETFS]
OTHER_GROUPS = ["Bond & gilt", "Gold & silver", "Liquid & money market", "Other ETFs"]
# Target-maturity ETFs that matured before Oct 2026 (and an old Bharat Bond): never offered.
MATURED = ["SDL26BEES", "AXISBPSETF", "EBBETF0425"]

# (symbol, text as NSE / Kotak describe it) - none of these may ever be counted as an index ETF.
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
        for n, (s, _name, isin) in enumerate(ALL_ROWS) if s not in skip
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
    etf = etfs.find(symbol)
    # offered in their own group (AXISBPSETF has matured and is gone)
    assert (etf is None) if symbol == "AXISBPSETF" else (etf.kind != "index" and etf.group in OTHER_GROUPS)


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
    assert len(cat) == len(INDEX_ETFS) + len(OTHER_ETFS)
    syms = _symbols(cat)
    assert len(syms) == len(set(syms))
    assert len({e.isin for e in cat}) == len(cat)
    for e in cat:
        assert e.trading_symbol == f"{e.symbol}-EQ"
        assert e.isin.startswith("INF")
        assert e.group in etfs.GROUPS and e.kind in etfs.KINDS
        assert e.name and "\t" not in e.name and "  " not in e.name and e.name == e.name.strip()
        assert e.exchange_segment == "nse_cm" and e.token is None
        if e.kind == "index":
            assert etfs.looks_like_index_etf(e.symbol, e.name, e.isin), e.symbol
            assert e.symbol not in NON_INDEX_SYMBOLS and e.group in etfs.INDEX_GROUPS
        else:
            assert not etfs.looks_like_index_etf(e.symbol, e.name, e.isin), e.symbol
            assert e.symbol in NON_INDEX_SYMBOLS and not e.core
            assert e.group == etfs.KIND_GROUPS[e.kind]
            # the readable name alone tells the kind
            assert etfs.guess_kind(e.symbol, e.name) == e.kind, e.symbol
    # every group has something in it
    assert {e.group for e in cat} == set(etfs.GROUPS)


def test_counts_per_kind(builtin):
    kinds = [e.kind for e in etfs.catalogue()]
    counts = {k: kinds.count(k) for k in etfs.KINDS}
    assert 200 <= counts["index"] <= 330
    assert counts["bond"] >= 15 and counts["gold"] >= 20 and counts["silver"] >= 15 and counts["liquid"] >= 15
    assert counts["other"] >= 1
    assert sum(counts.values()) == len(kinds)


def test_non_index_symbols_are_the_other_etfs_plus_gone_ones():
    other = {s for s, *_ in OTHER_ETFS}
    index = {s for s, *_ in INDEX_ETFS}
    assert other <= NON_INDEX_SYMBOLS
    assert not index & NON_INDEX_SYMBOLS
    # matured target-maturity ETFs and old symbols of renamed (gold / silver) ETFs
    assert NON_INDEX_SYMBOLS - other == {"SDL26BEES", "AXISBPSETF", "AXISGOLD", "AXISILVER"}


def test_readable_names_and_groups(builtin):
    assert etfs.find("NIFTYBEES").name == "Nifty 50"
    assert etfs.find("NIFTYBEES").kind == "index"
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
    # Within a group the most traded come first (POPULAR), then by symbol.
    keys = [(etfs.GROUPS.index(e.group), etfs._POPULAR_RANK.get(e.symbol, len(etfs.POPULAR)), e.symbol) for e in rest]
    assert keys == sorted(keys)


def test_index_etfs_come_before_all_other_etfs(builtin):
    kinds = [e.kind for e in etfs.catalogue()]
    n = len(INDEX_ETFS)
    assert kinds[:n] == ["index"] * n
    assert "index" not in kinds[n:]
    groups = [e.group for e in etfs.catalogue()[n:]]
    assert sorted(set(groups), key=groups.index) == OTHER_GROUPS


def test_groups_in_display_order():
    assert etfs.groups() == ["Broad market", "Nifty Next 50", "Midcap & Smallcap", "Bank & Financials", "Sensex",
                             "Sector & Theme", "Factor & Strategy", "International", "Bond & gilt", "Gold & silver",
                             "Liquid & money market", "Other ETFs"]


def test_to_dict(builtin):
    d = etfs.find("NIFTYBEES").to_dict()
    assert d == {"symbol": "NIFTYBEES", "trading_symbol": "NIFTYBEES-EQ", "name": "Nifty 50", "isin": "INF204KB14I2",
                 "group": "Broad market", "kind": "index", "core": True, "token": None, "exchange_segment": "nse_cm"}
    d = etfs.find("GOLDBEES").to_dict()
    assert d == {"symbol": "GOLDBEES", "trading_symbol": "GOLDBEES-EQ", "name": "Gold", "isin": "INF204KB17I5",
                 "group": "Gold & silver", "kind": "gold", "core": False, "token": None, "exchange_segment": "nse_cm"}


# ---- bond, gold, silver, liquid and other ETFs --------------------------------------------------------------------

@pytest.mark.parametrize("symbol,kind,group,name", [
    ("GOLDBEES", "gold", "Gold & silver", "Gold"),
    ("SILVERBEES", "silver", "Gold & silver", "Silver"),
    ("LIQUIDBEES", "liquid", "Liquid & money market", "Nifty 1D Rate (liquid)"),
    ("GILT5YBEES", "bond", "Bond & gilt", "Nifty 5yr Benchmark G-Sec"),
    ("EBBETF0430", "bond", "Bond & gilt", "Bharat Bond ETF \u2013 April 2030"),
    ("BBETF0432", "bond", "Bond & gilt", "Bharat Bond ETF \u2013 April 2032"),
    ("SETF10GILT", "bond", "Bond & gilt", "Nifty 10yr Benchmark G-Sec"),
    # NSE's file says "S&PBSEMidcapSelectIndex" for this one
    ("LTGILTBEES", "bond", "Bond & gilt", "Nifty 8-13 yr G-Sec"),
    ("CASHIETF", "liquid", "Liquid & money market", "BSE Liquid Rate"),
    ("HYBRIDETF", "other", "Other ETFs", "Nifty 200 Momentum 30 + 8-13 yr G-Sec (50:50 hybrid)"),
])
def test_other_etfs_are_offered_in_their_group(builtin, symbol, kind, group, name):
    for spelling in (symbol, symbol.lower(), f"{symbol}-EQ", f" {symbol.lower()}-eq "):
        e = etfs.find(spelling)
        assert e is not None and e.symbol == symbol, spelling
    assert (e.kind, e.group, e.name) == (kind, group, name)
    assert e.trading_symbol == f"{symbol}-EQ" and not e.core
    assert not etfs.is_index_etf(symbol)
    assert symbol in _symbols(etfs.catalogue())


def test_matured_target_maturity_etfs_are_gone(builtin):
    cat = etfs.catalogue()
    for s in MATURED:
        assert etfs.find(s) is None and s not in _symbols(cat)
    # whatever has a maturity year in its name matures in 2027 or later
    for e in cat:
        if e.kind == "bond":
            for year in re.findall(r"\b(20\d\d)\b", e.name):
                assert int(year) >= 2027, e
    assert {e.symbol for e in cat if e.name.startswith("Bharat Bond")} == {
        "EBBETF0430", "EBBETF0431", "BBETF0432", "EBBETF0433"}


def test_guess_kind():
    assert etfs.guess_kind("NEWGLDETF", "XYZAMC - GOLD ETF") == "gold"
    assert etfs.guess_kind("ABCSLVR", "") == "silver"
    assert etfs.guess_kind("CASHETF2", "XYZAMC - CASHETF2") == "liquid"
    assert etfs.guess_kind("NEWOVN", "XYZ CRISIL Overnight ETF") == "liquid"
    assert etfs.guess_kind("NEWMM", "XYZ Money Market ETF") == "liquid"
    assert etfs.guess_kind("SDL28ETF", "") == "bond"
    assert etfs.guess_kind("TBILLETF", "Nifty 1Y T-Bill") == "bond"
    assert etfs.guess_kind("NEWDEBT", "XYZ CRISIL IBX AAA Index Jun 2027") == "bond"
    assert etfs.guess_kind("BBETF0440", "") == "bond"
    assert etfs.guess_kind("NEWGSEC", "XYZ Nifty 4-8 yr G-Sec ETF") == "bond"
    assert etfs.guess_kind("ARBETF", "XYZ Arbitrage ETF") == "other"
    assert etfs.guess_kind("NEWHYB", "XYZ Nifty 50 + G-Sec Hybrid ETF") == "other"
    assert etfs.guess_kind("PLAIN", "XYZAMC - PLAIN") == "other"
    assert etfs.guess_kind("NEWCPSE", "Goldman Sachs CPSE") == "other"
    assert etfs.guess_kind("HOUSING", "Nifty Housing Sector") == "other"  # "g Sec-tor" is not G-Sec
    for s, name, _isin, kind in OTHER_ETFS:
        assert etfs.guess_kind(s, name) == kind, s


# ---- is_index_etf / search --------------------------------------------------------------------------------------

def test_is_index_etf_accepts_any_spelling_of_the_symbol(builtin):
    assert etfs.is_index_etf("NIFTYBEES-EQ")
    assert etfs.is_index_etf("niftybees")
    assert etfs.is_index_etf(" NiftyBees ", "Nifty 50")
    assert not etfs.is_index_etf("RELIANCE")
    assert not etfs.is_index_etf("GOLDBEES-EQ") and etfs.find("GOLDBEES-EQ") is not None
    assert not etfs.is_index_etf("")
    assert etfs.find("RELIANCE") is None and etfs.find("") is None


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
    assert len(etfs.search("nifty", limit=7)) == 7


def _by_kind(kind):
    return [e for e in etfs.catalogue() if e.kind == kind]


def test_search_bond(builtin):
    found = etfs.search("bond", limit=100)
    # every bond / gilt ETF and nothing else; the Bharat Bond ETFs (bond in the name) first
    assert {e.symbol for e in found} == {e.symbol for e in _by_kind("bond")}
    assert {e.symbol for e in found[:4]} == {"EBBETF0430", "EBBETF0431", "BBETF0432", "EBBETF0433"}
    assert etfs.search("bharat bond")[0].name.startswith("Bharat Bond")
    assert etfs.search("april 2032")[0].symbol == "BBETF0432"
    gsec = _symbols(etfs.search("gsec", limit=100))
    assert "GILT5YBEES" in gsec and "SETF10GILT" in gsec and "MOGSEC" in gsec
    assert set(_symbols(etfs.search("g-sec", limit=100))) >= {"GILT5YBEES", "SETF10GILT", "LTGILTBEES"}
    assert etfs.search("gilt5ybees-eq")[0].symbol == "GILT5YBEES"


def test_search_gold_and_liquid(builtin):
    found = etfs.search("gold", limit=100)
    golds = {e.symbol for e in _by_kind("gold")}
    assert "GOLDBEES" in golds
    # all gold ETFs first; silver ones (same group) may follow; never an index ETF
    assert {e.symbol for e in found[:len(golds)]} == golds
    assert {e.kind for e in found[len(golds):]} <= {"silver"}
    assert etfs.search("goldbees")[0].symbol == "GOLDBEES"
    liquid = etfs.search("liquid", limit=100)
    assert liquid and {e.kind for e in liquid} == {"liquid"}
    assert {e.symbol for e in liquid} == {e.symbol for e in _by_kind("liquid")}
    assert etfs.search("liquidbees")[0].symbol == "LIQUIDBEES"
    assert etfs.search("silverbees")[0].symbol == "SILVERBEES"


def test_search_by_group(builtin):
    found = etfs.search("", limit=500, group="Gold & silver")
    assert found and {e.kind for e in found} == {"gold", "silver"}
    assert len(found) == len(_by_kind("gold")) + len(_by_kind("silver"))
    assert found == [e for e in etfs.catalogue() if e.group == "Gold & silver"]  # catalogue order
    assert len(etfs.search("", limit=5, group="Gold & silver")) == 5
    bonds = etfs.search("nifty", limit=100, group="Bond & gilt")
    assert bonds and all(e.kind == "bond" for e in bonds)
    assert etfs.search("goldbees", group="Bond & gilt") == []
    assert _symbols(etfs.search("", group="Broad market")[:2]) == ["NIFTYBEES", "SETFNIF50"]
    assert etfs.search("", group="No such group") == []
    # no group (None or "") means every group
    assert etfs.search("", group="") == etfs.search("") == etfs.search("", group=None)


# ---- live Kotak list ----------------------------------------------------------------------------------------------

def test_live_list_merge(monkeypatch):
    gone = {"HDFCNIMEG", "MOMMIDCAP", "NIFTYAXIS", "GOLDCASE", "EBBETF0431"}
    isin_of = {s: isin for s, _n, isin in ALL_ROWS}
    rows = _live_rows(skip=gone) + [
        # a new index ETF that only Kotak knows about
        Instrument(symbol="DEFENCENEW", trading_symbol="DEFENCENEW-EQ", name="XYZAMC - DEFENCENEW", token="777",
                   isin="INF999X01011"),
        # new non-index ETFs that only Kotak knows about: offered with a guessed kind
        Instrument(symbol="NEWGLDETF", trading_symbol="NEWGLDETF-EQ", name="XYZAMC - GOLD ETF", token="778",
                   isin="INF999X01029"),
        Instrument(symbol="CASHETF2", trading_symbol="CASHETF2-EQ", name="XYZAMC - CASHETF2", token="780",
                   isin="INF999X01037"),
        Instrument(symbol="SDL28ETF", trading_symbol="SDL28ETF-EQ", name="XYZAMC - SDL28ETF", token="783",
                   isin="INF999X01052"),
        Instrument(symbol="ARBETF", trading_symbol="ARBETF-EQ", name="XYZAMC - ARBITRAGE ETF", token="784",
                   isin="INF999X01060"),
        # a company share: never offered
        Instrument(symbol="RELIANCE", trading_symbol="RELIANCE-EQ", name="RELIANCE INDUSTRIES LTD", token="2885",
                   isin="INE002A01018"),
        # a matured SDL ETF still in a stale list, and GOLDAXIS's old symbol: never offered
        Instrument(symbol="SDL26BEES", trading_symbol="SDL26BEES-EQ", name="RELCAPAMC-SDL26BEES", token="785",
                   isin="INF204KC1022"),
        Instrument(symbol="AXISGOLD", trading_symbol="AXISGOLD-EQ", name="AXISAMC-AXISGOLD", token="786",
                   isin=isin_of["GOLDAXIS"]),
        # the same fund as NIFTYAXIS under its old symbol: keeps the readable name
        Instrument(symbol="AXISNIFTY", trading_symbol="AXISNIFTY-EQ", name="AXISAMC-AXISNIFTY", token="781",
                   isin=isin_of["NIFTYAXIS"]),
        # block-deal series rows are not tradable for us
        Instrument(symbol="BLOCKETF", trading_symbol="BLOCKETF-BL", name="XYZAMC - BLOCKETF", token="782",
                   isin="INF999X01045", series="BL"),
    ]
    monkeypatch.setattr(etfs, "scrip_master", FakeScrips(rows))

    cat = etfs.catalogue()
    by = {e.symbol: e for e in cat}
    # built-in rows missing from the live list are dropped (index and other ETFs alike)
    assert not gone & set(by)
    assert len(cat) == len(ALL_ROWS) - len(gone) + 1 + 4 + 1  # DEFENCENEW, 4 new non-index ETFs, AXISNIFTY
    # the new index ETF is added with Kotak's name and a guessed group
    new = by["DEFENCENEW"]
    assert new.token == "777" and new.name == "XYZAMC - DEFENCENEW" and new.group == "Sector & Theme"
    assert new.trading_symbol == "DEFENCENEW-EQ" and new.isin == "INF999X01011" and not new.core
    assert new.kind == "index" and etfs.is_index_etf("defencenew-eq")
    # new non-index ETFs get a guessed kind and its group
    for s, kind, group, token in [("NEWGLDETF", "gold", "Gold & silver", "778"),
                                  ("CASHETF2", "liquid", "Liquid & money market", "780"),
                                  ("SDL28ETF", "bond", "Bond & gilt", "783"),
                                  ("ARBETF", "other", "Other ETFs", "784")]:
        e = by[s]
        assert (e.kind, e.group, e.token, e.trading_symbol) == (kind, group, token, f"{s}-EQ"), s
        assert e.name.startswith("XYZAMC - ") and not e.core
        assert not etfs.is_index_etf(s) and etfs.find(s.lower()) == e
    # never offered
    for s in ("RELIANCE", "SDL26BEES", "AXISGOLD", "BLOCKETF"):
        assert s not in by and etfs.find(s) is None
    # renamed fund found by ISIN; a symbol match wins over an ISIN match
    assert by["AXISNIFTY"].name == "Nifty 50" and by["AXISNIFTY"].token == "781"
    tokens = {s: str(10000 + n) for n, (s, *_rest) in enumerate(ALL_ROWS)}
    assert by["GOLDAXIS"].token == tokens["GOLDAXIS"]
    # tokens are filled from the live list
    assert by["NIFTYBEES"].token == tokens["NIFTYBEES"]
    assert by["GOLDBEES"].token == tokens["GOLDBEES"] and by["GOLDBEES"].name == "Gold"
    assert by["LIQUIDBEES"].token == tokens["LIQUIDBEES"] and by["LIQUIDBEES"].kind == "liquid"
    assert all(e.token for e in cat)
    # still core first, in order; index ETFs before all others; others by group, then symbol
    assert _symbols(cat[:len(CORE_SYMBOLS)]) == CORE_SYMBOLS
    kinds = [e.kind for e in cat]
    n_index = kinds.count("index")
    assert "index" not in kinds[n_index:]
    keys = [(etfs.GROUPS.index(e.group), etfs._POPULAR_RANK.get(e.symbol, len(etfs.POPULAR)), e.symbol) for e in cat[n_index:]]
    assert keys == sorted(keys)


def test_builtin_rows_shown_without_tokens_until_live_list_loads(builtin):
    cat = etfs.catalogue()
    assert len(cat) == len(INDEX_ETFS) + len(OTHER_ETFS)
    assert all(e.token is None for e in cat)


def test_broken_live_list_falls_back_to_builtin(monkeypatch):
    rows = _live_rows()[:5]
    monkeypatch.setattr(etfs, "scrip_master", FakeScrips(rows))
    cat = etfs.catalogue()
    assert len(cat) == len(INDEX_ETFS) + len(OTHER_ETFS)
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


def test_most_traded_gold_silver_and_liquid_etfs_come_first():
    from app.services import etfs

    assert etfs.search("gold")[0].symbol == "GOLDBEES"
    assert "SILVERBEES" in [e.symbol for e in etfs.search("silver")[:2]]  # after an exact "SILVER" match
    assert "LIQUIDBEES" in [e.symbol for e in etfs.search("liquid")[:2]]  # after an exact "LIQUID" match
    gold = etfs.search("", 400, group="Gold & silver")
    assert [e.symbol for e in gold[:2]] == ["GOLDBEES", "SILVERBEES"]
