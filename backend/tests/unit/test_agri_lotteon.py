"""롯데온 연동: 등록 전 점검, 요청 내용 만들기, 실제 클라이언트가 보내는 모양 (롯데온에는 아무것도 보내지 않는다)."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.integrations.markets.lotteon import (
    DispatchItem,
    LotteonClaim,
    LotteonClaimItem,
    LotteonClient,
    LotteonError,
    MockLotteonClient,
    _parse_order,
    kst_stamp,
    split_key,
)
from app.services import agri_lotteon as lo
from app.services import agri_lotteon_orders as lo_orders
from app.services import agri_pricing as pricing


def _settings():
    settings = pricing.merge_settings(None)
    settings["cs_phone"] = "1588-0000"
    box = settings["lotteon"]
    box.update(tr_no="LO100", owhp_no="PLO1", rtrp_no="PLO2", dv_cst_pol_no="DLD1", dv_rgspr_grp_cd="GN101")
    box["standard_category_ids"]["fruit"] = "BC63080300"
    box["display_category_ids"]["fruit"] = "FC11130203"
    box["standard_category_ids"]["rice"] = "BC1"
    box["display_category_ids"]["rice"] = "FC1"
    return settings


def _option(option_id, label, supply, soldout=False):
    return SimpleNamespace(
        option_id=option_id, label=label, supply_price=supply, shipping_fee=0, sale_price=None,
        is_soldout=soldout, supplier_code=None, weight=None, count=None, grade=None,
    )


def _product(**overrides):
    data = dict(
        product_id=7, name="충주 사과", category="fruit", origin="국산(충북 충주)", producer="충주농원",
        storage="냉장 보관", packing_info=None, variety=None, harvest_year=None, grade=None,
        image_url="https://example.com/apple.jpg", status="ready",
        options=[_option(11, "3kg", 12000), _option(12, "5kg", 18000)],
    )
    data.update(overrides)
    return SimpleNamespace(**data)


def test_complete_product_has_no_problems():
    assert lo.collect_problems(_product(), _settings()) == []


def test_blank_settings_list_what_is_missing():
    text = " ".join(lo.collect_problems(_product(), pricing.merge_settings(None)))
    for needle in ("거래처번호", "표준카테고리", "전시카테고리", "출고지번호", "회수(반품)지번호", "배송비정책번호", "배송가능지역코드", "고객센터"):
        assert needle in text
    assert "상품정보고시" not in text  # 농수축산물 항목코드는 PDF에서 확인해 기본값으로 넣어 두었다
    cleared = _settings()
    cleared["lotteon"]["notice_codes"] = {k: "" for k in cleared["lotteon"]["notice_codes"]}
    assert any("상품정보고시" in p for p in lo.collect_problems(_product(), cleared))


def test_import_origin_needs_import_code_and_rice_needs_label_fields():
    assert any("수입" in p for p in lo.collect_problems(_product(origin="미국"), _settings()))
    rice = " ".join(lo.collect_problems(_product(category="rice"), _settings()))
    assert "품종" in rice and "생산연도" in rice and "등급" in rice


def test_low_price_option_blocks_registration():
    product = _product(options=[SimpleNamespace(**{**_option(11, "3kg", 12000).__dict__, "sale_price": 12500})])
    assert any("최소 이익" in p for p in lo.collect_problems(product, _settings()))


def test_payload_has_required_fields_with_per_option_prices():
    settings = _settings()
    payload = lo.build_payload(_product(), settings)
    assert payload["epdNo"] == "agri-7" and payload["scatNo"] == "BC63080300"
    assert payload["dcatLst"] == [{"mallCd": "LTON", "lfDcatNo": "FC11130203"}]
    assert payload["oplcCd"] == "KR" and payload["tdfDvsCd"] == "02"
    assert payload["pdItmsInfo"]["pdItmsCd"] == "20"
    articles = {a["pdArtlCd"]: a["pdArtlCnts"] for a in payload["pdItmsInfo"]["pdItmsArtlLst"]}
    assert set(articles) == {"1460", "0570", "0610", "1730", "1760", "1680", "1690", "1700", "0560", "0590", "1470", "0510"}
    assert articles["1460"] == "충주 사과" and articles["1730"] == "국산(충북 충주)" and articles["0510"] == "1588-0000"
    assert articles["0590"] == "냉장 보관" and articles["1700"] == "해당 없음"
    assert payload["dvProcTypCd"] == "LO_ENTP" and payload["dvPdTypCd"] == "GNRL" and payload["sndBgtNday"] == 2
    assert payload["owhpNo"] == "PLO1" and payload["rtrpNo"] == "PLO2" and payload["dvCstPolNo"] == "DLD1"
    assert payload["saveTypCd"] == "RFRG"
    assert payload["sitmYn"] == "Y" and len(payload["itmLst"]) == 2
    first, second = payload["itmLst"]
    assert first["eitmNo"] == "agri-11" and second["eitmNo"] == "agri-12"
    assert first["slPrc"] < second["slPrc"] and first["itmOptLst"] == [{"optNm": "옵션", "optVal": "3kg"}]
    assert first["itmImgLst"][0]["origImgFileNm"] == "https://example.com/apple.jpg"
    assert payload["optSrtLst"][0]["optValSrtLst"][1] == {"optValSeq": 2, "optVal": "5kg"}
    assert payload["dpYn"] == "N"  # 처음에는 전시하지 않는다


def test_single_option_product_is_not_split_into_options():
    payload = lo.build_payload(_product(options=[_option(11, "3kg", 12000)]), _settings())
    assert payload["sitmYn"] == "N" and "optSrtLst" not in payload and "itmOptLst" not in payload["itmLst"][0]


def test_display_on_register_and_storage_codes():
    settings = _settings()
    settings["lotteon"]["display_on_register"] = True
    assert lo.build_payload(_product(storage="냉동"), settings)["dpYn"] == "Y"
    assert lo.build_payload(_product(storage="냉동"), settings)["saveTypCd"] == "FRZN"
    assert lo.build_payload(_product(storage=None), settings)["saveTypCd"] == "NONE"


# ---------------------------------------------------------------- 보조 함수


def test_split_key_and_courier_codes():
    assert split_key("MOCK-AB12-3-2") == ("MOCK-AB12", 3, 2)
    assert split_key("2026110512345-1-1") == ("2026110512345", 1, 1)
    assert lo_orders.courier_code("CJ대한통운") == "0002"
    assert lo_orders.courier_code("cj 대한통운") == "0002"
    assert lo_orders.courier_code("0006") == "0006"
    assert lo_orders.courier_code("이상한택배") is None


def test_parse_order_reads_delivery_fields():
    row = {
        "odNo": "2026110512345", "odSeq": 2, "procSeq": 1, "odTypCd": "10", "dvRtrvDvsCd": "DV", "odPrgsStepCd": "11",
        "epdNo": "agri-7", "eitmNo": "agri-12", "spdNm": "충주 사과", "sitmNm": "5kg", "odQty": 2, "slAmt": 36000,
        "odrNm": "김영희", "mphnNo": "010-1", "dvpCustNm": "김철수", "dvpMphnNo": "010-2", "dvpZipNo": "12345",
        "dvpStnmZipAddr": "서울시 강남구", "dvpStnmDtlAddr": "101동", "dvMsg": "문 앞",
    }
    order = _parse_order(row)
    assert order.key == "2026110512345-2-1" and order.quantity == 2 and order.amount == 36000
    assert order.receiver_name == "김철수" and order.address == "서울시 강남구 101동" and order.eitm_no == "agri-12"


def test_kst_stamp_is_korean_time():
    assert kst_stamp(datetime(2026, 11, 5, 0, 0, 0, tzinfo=timezone.utc)) == "20261105090000"


async def test_mock_client_flow():
    client = MockLotteonClient()
    order = client.add_order(epd_no="agri-1", eitm_no="agri-1")
    assert [o.key for o in await client.fetch_orders(datetime.now(), datetime.now())] == [order.key]
    await client.complete_interface([order])
    assert await client.fetch_orders(datetime.now(), datetime.now()) == []  # 연동완료한 주문은 다시 안 나온다
    claim = client.add_cancel_request(order)
    assert claim.items[0].orgl_proc_seq == order.proc_seq and claim.items[0].step == "02"


# ---------------------------------------------------------------- 실제 클라이언트가 보내는 모양


def _real(handler):
    return LotteonClient("KEY123", transport=httpx.MockTransport(handler))


async def test_real_client_sends_required_headers_and_reads_identity():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(path=request.url.path, headers=dict(request.headers))
        return httpx.Response(200, json={"returnCode": "0000", "data": {"trGrpCd": "SR", "trNo": "LO100", "trNm": "내 상점"}})

    data = await _real(handler).identity()
    assert data["trNo"] == "LO100" and seen["path"] == "/v1/openapi/common/v1/identity"
    assert seen["headers"]["authorization"] == "Bearer KEY123" and seen["headers"]["x-timezone"] == "GMT+09:00"
    assert seen["headers"]["accept-language"] == "ko"


@pytest.mark.parametrize("status,needle", [(401, "인증키"), (403, "IP"), (429, "횟수")])
async def test_real_client_explains_auth_and_ip_problems(status, needle):
    with pytest.raises(LotteonError) as exc:
        await _real(lambda r: httpx.Response(status, json={})).identity()
    assert needle in str(exc.value)


async def test_real_register_reads_per_product_result():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/openapi/product/v1/product/registration/request"
        assert json.loads(request.content)["spdLst"][0]["epdNo"] == "agri-7"
        return httpx.Response(
            200,
            json={"returnCode": "0000", "data": [
                {"epdNo": "agri-7", "spdNo": "LO555", "resultCode": "0000", "resultMessage": ""},
                {"epdNo": "agri-8", "spdNo": None, "resultCode": "1001", "resultMessage": "[원산지코드]는 필수입력입니다."},
            ]},
        )

    ok, bad = await _real(handler).register_products([{"epdNo": "agri-7"}])
    assert ok.ok and ok.spd_no == "LO555"
    assert not bad.ok and "원산지코드" in bad.message


async def test_real_fetch_orders_uses_one_day_windows_and_unfinished_filter():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        rows = [{"odNo": "100", "odSeq": 1, "procSeq": 1, "dvpCustNm": "a", "dvpStnmZipAddr": "b"}] if len(bodies) == 1 else []
        return httpx.Response(200, json={"returnCode": "0000", "data": {"deliveryOrderList": rows}})

    now = datetime.now(timezone.utc)
    orders = await _real(handler).fetch_orders(now - timedelta(hours=40), now)
    assert len(orders) == 1 and len(bodies) == 2  # 하루 넘는 기간은 나눠서 부른다
    assert all(b["ifCplYN"] == "N" for b in bodies)


async def test_real_dispatch_sends_expected_body_and_reads_fail_list():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"returnCode": "0000", "data": {"failList": [{"odNo": "200", "odSeq": 1, "procSeq": 1, "rsltCd": "3021", "rsltMsg": "취소 상태"}]}},
        )

    ok, bad = await _real(handler).dispatch(
        [DispatchItem("100", 1, 1, "0002", "111", 2), DispatchItem("200", 1, 1, "0002", "222", 1)]
    )
    assert seen["path"] == "/v1/openapi/delivery/v2/SellerDeliveryProgressStateInform"
    first = seen["body"]["deliveryProgressStateList"][0]
    assert first["odPrgsStepCd"] == "13" and first["dvCoCd"] == "0002" and first["invcNoList"] == ["111"]
    assert first["invcNbr"] == 1 and first["slQty"] == 2 and len(first["dvTrcStatDttm"]) == 14
    assert ok.ok and not bad.ok and "3021" in bad.message  # returnCode 가 0000 이어도 건별 실패를 읽는다


async def test_real_delay_inform_sends_list_body_with_reason_and_date():
    from app.integrations.markets.lotteon import DelayItem

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"returnCode": "0000", "data": {"rsltCd": "0000"}})

    due = datetime(2026, 11, 9, 12, 0, tzinfo=timezone(timedelta(hours=9)))
    [outcome] = await _real(handler).inform_delay([DelayItem("100", 1, 1, "STK_SHTG", due)])
    assert outcome.ok and seen["path"] == "/v1/openapi/delivery/v1/SellerDeliveryAppointmentInform"
    assert isinstance(seen["body"], list)  # 설명서 예시: 최상위가 목록
    assert seen["body"][0] == {
        "odNo": "100", "odSeq": 1, "procSeq": 1, "orglProcSeq": "", "clmNo": "", "sndDlRsnCd": "STK_SHTG",
        "sndDlRsnCnts": "", "sndAgrdDttm": "20261109120000",
    }


async def test_real_delay_over_max_period_is_reported():
    from app.integrations.markets.lotteon import DelayItem

    handler = lambda r: httpx.Response(200, json={"returnCode": "2004", "message": "발송약속일 최대기간 초과"})  # noqa: E731
    with pytest.raises(LotteonError) as exc:
        await _real(handler).inform_delay([DelayItem("100", 1, 1, "STK_SHTG", datetime.now(timezone.utc))])
    assert "2004" in str(exc.value) and "최대기간" in str(exc.value)


async def test_real_check_sendable_treats_6001_as_not_sendable():
    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {"odNo": "100", "odSeq": "1", "procSeq": "1"}
        return httpx.Response(200, json={"returnCode": "6001", "message": "발송처리가 불가능합니다"})

    outcome = await _real(handler).check_sendable("100", 1, 1)
    assert not outcome.ok and "취소" in outcome.message


async def test_real_cancel_claims_and_approval_body():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.url.path.rsplit("/", 1)[-1], body))
        if request.url.path.endswith("getCancellationRequestAndComplateList"):
            return httpx.Response(
                200,
                json={"returnCode": "0000", "data": [{"odNo": "100", "clmNo": "C1", "itemList": [
                    {"odSeq": 1, "procSeq": 2, "orglProcSeq": 1, "odPrgsStepCd": "02", "clmRsnCd": "106"}]}]},
            )
        return httpx.Response(200, json={"returnCode": "0000"})

    client = _real(handler)
    now = datetime.now(timezone.utc)
    [claim] = await client.fetch_cancel_claims(now - timedelta(days=3), now, "100")
    assert claim.clm_no == "C1" and claim.items[0].orgl_proc_seq == 1 and claim.items[0].step == "02"
    assert seen[0][1]["odNo"] == "100"
    await client.approve_cancel(claim, "106")
    await client.hold_cancel(claim, "901")
    assert seen[1][0] == "cnclRequestApproval" and seen[2][0] == "cnclRequestHold"
    item = seen[1][1]["itemList"][0]
    assert seen[1][1]["clmNo"] == "C1" and item == {"odSeq": 1, "procSeq": 2, "orglProcSeq": 1, "slrRsnCd": "106"}
    assert seen[2][1]["itemList"][0]["slrRsnCd"] == "901"


async def test_real_client_turns_business_error_into_readable_message():
    with pytest.raises(LotteonError) as exc:
        await _real(lambda r: httpx.Response(200, json={"returnCode": "3006", "message": "주문의 상태를 확인해 주세요"})).approve_cancel(
            LotteonClaim("100", "C1", [LotteonClaimItem(1, 2, 1, "02")]), "106"
        )
    assert "3006" in str(exc.value) and "주문의 상태" in str(exc.value)


def test_real_client_requires_key():
    with pytest.raises(ValueError):
        LotteonClient("")
