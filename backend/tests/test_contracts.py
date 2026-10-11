"""F&O and MCX contract lists: decoding Kotak's scrip master and searching it."""

from datetime import date, datetime, timezone

from app.services import contracts
from app.services.contracts import ContractMaster, decode_expiry, parse_contract_csv, sample_contracts

TODAY = date(2026, 10, 14)


def _epoch(d: date, hour: int, minute: int = 0, second: int = 0) -> int:
    return int(datetime(d.year, d.month, d.day, hour, minute, second, tzinfo=timezone.utc).timestamp())


def _nse(d: date) -> int:
    return _epoch(d, 14, 30) - contracts.NSE_EPOCH_OFFSET  # nse_fo counts from 1980


HEADER = ("pSymbol,pExchSeg,pTrdSymbol,pSymbolName,pInstType,pOptionType,dStrikePrice;,lLotSize,"
          "lExpiryDate ,dTickSize ,lMultiplier ,dGenNum,dGenDen\n")


def _csv(rows: list[str]) -> bytes:
    return (HEADER + "\n".join(rows) + "\n").encode()


def test_nse_rows_decode_expiry_strike_and_lots():
    rows = [
        f"35001,nse_fo,NIFTY26OCTFUT,NIFTY,FUTIDX,XX,-1,65,{_nse(date(2026, 10, 27))},5,-1,1,1",
        f"35002,nse_fo,NIFTY26O2025000CE,NIFTY,OPTIDX,CE,2500000,65,{_nse(date(2026, 10, 20))},5,-1,1,1",
        f"35003,nse_fo,NIFTY26O0725000PE,NIFTY,OPTIDX,PE,2500000,65,{_nse(date(2026, 10, 7))},5,-1,1,1",  # expired
        f"53032 72263,nse_fo,NESTLE27OCT24NOVFUT,NESTLEIND,FUTSTK,XX,-1,250,{_nse(date(2026, 10, 27))},5,-1,1,1",
        f"35002,nse_fo,NIFTY26O2025000CE,NIFTY,OPTIDX,CE,2500000,65,{_nse(date(2026, 10, 20))},5,-1,1,1",  # duplicate
    ]
    out = parse_contract_csv(_csv(rows), "nse_fo", TODAY)
    assert [c.trading_symbol for c in out] == ["NIFTY26OCTFUT", "NIFTY26O2025000CE"]
    fut, opt = out
    assert (fut.expiry, fut.option_type, fut.strike, fut.lot_size, fut.kind) == (date(2026, 10, 27), None, None, 65,
                                                                               "future")
    assert (opt.expiry, opt.option_type, opt.strike, opt.tick_size) == (date(2026, 10, 20), "CE", 25000.0, 0.05)
    assert opt.display == "NIFTY 20 OCT 25000 CE"
    assert fut.display == "NIFTY 27 OCT FUT"
    assert "strike ₹25,000" in opt.description and "lot 65" in opt.description


def test_bse_and_mcx_use_plain_unix_time_and_their_own_types():
    bse = [f"1101,bse_fo,SENSEX26OCT82000CE,SENSEX,IO,CE,8200000,20,{_epoch(date(2026, 10, 15), 10)},5,-1,1,1",
           f"1102,bse_fo,SENSEX26OCTFUT,SENSEX,IF,XX,-1,20,{_epoch(date(2026, 10, 29), 10)},5,-1,1,1"]
    out = parse_contract_csv(_csv(bse), "bse_fo", TODAY)
    assert [(c.instrument_type, c.expiry) for c in out] == [("OPTIDX", date(2026, 10, 15)),
                                                            ("FUTIDX", date(2026, 10, 29))]
    mcx = [f"2201,mcx_fo,GOLD26DECFUT,GOLD,FUTCOM,XX,-1,1,{_epoch(date(2026, 12, 4), 23, 59, 59)},100,-1,100,1",
           f"2202,mcx_fo,GOLDM26NOVFUT,GOLDM,FUTCOM,XX,-1,100,{_epoch(date(2026, 11, 5), 23, 59, 59)},100,-1,1,10",
           "2203,mcx_fo,GOLD,GOLD,COM,XX,-1,1,0,100,-1,1,1",  # spot price row: not tradable
           f"2204,mcx_fo,CRUDEOIL26O2005800CE,CRUDEOIL,OPTFUT,CE,580000,100,{_epoch(date(2026, 10, 15), 23, 59, 59)},"
           "10,-1,1,1"]
    out = parse_contract_csv(_csv(mcx), "mcx_fo", TODAY)
    by = {c.symbol: c for c in out}
    assert set(by) == {"GOLD", "GOLDM", "CRUDEOIL"}
    assert by["GOLD"].expiry == date(2026, 12, 4)  # 23:59:59 UTC is still the 4th
    assert (by["GOLD"].lot_size, by["GOLD"].multiplier) == (1, 100.0)
    assert (by["GOLDM"].lot_size, by["GOLDM"].multiplier) == (100, 0.1)
    assert by["CRUDEOIL"].strike == 5800.0 and by["CRUDEOIL"].tick_size == 0.1


def test_expiry_sentinels_mean_none():
    assert decode_expiry("-1", "nse_fo") is None and decode_expiry("0", "mcx_fo") is None
    assert decode_expiry("", "bse_fo") is None


def _master_with_sample(monkeypatch):
    m = ContractMaster()
    sample = sample_contracts(TODAY)
    for seg in contracts.SEGMENTS:
        m._set(seg, sample[seg], "sample", TODAY.isoformat())
    monkeypatch.setattr(m, "ensure_loaded", lambda seg, force=False: None)
    monkeypatch.setattr(contracts, "_today", lambda: TODAY)
    return m


def test_search_ranks_exact_underlying_and_nearest_expiry(monkeypatch):
    m = _master_with_sample(monkeypatch)
    res = m.search("nifty 25000 ce", ("nse_fo", "bse_fo"))
    assert res and all(c.symbol == "NIFTY" and c.strike == 25000 and c.option_type == "CE" for c in res)
    assert [c.expiry for c in res] == sorted(c.expiry for c in res)
    res = m.search("nifty", ("nse_fo",))
    assert res[0].symbol == "NIFTY" and res[0].is_future  # futures first without a strike or CE/PE
    assert m.search("banknifty fut", ("nse_fo",))[0].symbol == "BANKNIFTY"
    gold = m.search("gold", ("mcx_fo",))
    assert {c.symbol for c in gold} == {"GOLD", "GOLDM"} and gold[0].symbol == "GOLD"
    assert {c.symbol for c in m.search("crude", ("mcx_fo",))} == {"CRUDEOIL", "CRUDEOILM"}
    # Futures of every matching commodity come before options, even after an exact match's options.
    crude = m.search("crudeoil", ("mcx_fo",))
    futures = [c for c in crude if c.is_future]
    assert {c.symbol for c in futures} == {"CRUDEOIL", "CRUDEOILM"} and crude[:len(futures)] == futures
    assert all(c.is_option for c in m.search("crudeoil 5500", ("mcx_fo",)))
    assert m.search("silverm", ("nse_fo",)) == []


def test_get_refuses_unknown_and_expired(monkeypatch):
    m = _master_with_sample(monkeypatch)
    fut = m.search("nifty fut", ("nse_fo",))[0]
    assert m.get("nse_fo", fut.trading_symbol) is fut
    assert m.get("nse_fo", "NOPE") is None and m.get("cde_fo", fut.trading_symbol) is None
    monkeypatch.setattr(contracts, "_today", lambda: date(2027, 6, 1))
    assert m.get("nse_fo", fut.trading_symbol) is None


def test_sample_list_has_future_weekday_expiries():
    sample = sample_contracts(TODAY)
    for seg in contracts.SEGMENTS:
        assert sample[seg], seg
        assert all(c.expiry >= TODAY and c.expiry.weekday() < 5 and c.lot_size >= 1 for c in sample[seg])
        assert len({c.token for c in sample[seg]}) == len(sample[seg])


def test_disk_cache_round_trip(tmp_path, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    monkeypatch.setattr(contracts, "_today", lambda: TODAY)
    m = ContractMaster()
    rows = sample_contracts(TODAY)["nse_fo"][:5]
    m._cache_dir().joinpath(f"nse_fo-{TODAY.isoformat()}.json").write_text(
        __import__("json").dumps([c.to_dict() for c in rows]))
    assert m._load_from_disk("nse_fo", TODAY.isoformat()) is True
    assert m.status("nse_fo") == {"source": "kotak", "count": 5, "loaded_on": TODAY.isoformat(), "message": None}
    assert m._segs["nse_fo"].by_symbol[rows[0].trading_symbol].expiry == rows[0].expiry


def test_paper_margin_and_pnl_use_the_mcx_multiplier(monkeypatch):
    from app.broker.paper import estimate_margin

    m = _master_with_sample(monkeypatch)
    gold = m.search("gold fut", ("mcx_fo",))[0]
    assert gold.symbol == "GOLD" and gold.multiplier == 100.0
    monkeypatch.setattr("app.services.contracts.contract_master", m)
    from app.broker.paper import contract_multiplier
    assert contract_multiplier("mcx_fo", gold.trading_symbol) == 100.0
    assert contract_multiplier("nse_fo", "NIFTY26OCTFUT") == 1.0
    assert estimate_margin(gold.trading_symbol, 120000.0, 1, "S", "NRML", 100.0) == 120000.0 * 100 * 0.15
