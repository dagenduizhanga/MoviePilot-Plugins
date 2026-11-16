"""
纯函数测试：金额解析、面额匹配、兑换码提取、敏感信息清理等。
"""
from app.plugins.oyunforgiftcard.browser import playwright_proxy
from app.plugins.oyunforgiftcard.shop import (amount_pattern, extract_codes, find_money, fold, load_overrides,
                                               mask_code, parse_cookie, parse_expiry, parse_number, pick_card,
                                               sanitize_html)


def test_fold_turkish():
    assert fold("  SATIN AL  ") == "satin al"
    assert fold("Ödemeye Geç") == "odemeye gec"
    assert fold("Kredi Kartı / Banka Kartı") == "kredi karti / banka karti"
    assert fold("İŞLEM BAŞARILI") == "islem basarili"


def test_parse_number_formats():
    assert parse_number("1.234,56") == 1234.56
    assert parse_number("1,234.56") == 1234.56
    assert parse_number("262,50") == 262.5
    assert parse_number("1.250") == 1250
    assert parse_number("250") == 250
    assert parse_number("250.00") == 250
    assert parse_number("") is None


def test_find_money():
    assert find_money("Bakiye: 1.500,00 ₺") == [1500.0]
    assert find_money("Genel Toplam: 262,50 TL") == [262.5]
    assert find_money("₺1.072,50 ve 99 TL") == [1072.5, 99.0]
    assert find_money("Sepetim (2)") == []


def test_amount_pattern_does_not_confuse_250_and_2500():
    pattern = amount_pattern(250)
    assert pattern.search("250 tl tutarinda app store")
    assert pattern.search("app store 250,00 ₺")
    assert pattern.search("/250-tl-tutarinda")
    assert not pattern.search("2500 tl tutarinda")
    assert not pattern.search("1250 tl")


def test_extract_codes():
    text = "Kodunuz: X01001QZ1ABCDEFG\nSipariş No 123456789012\nxlowercase0000000"
    assert extract_codes(text) == ["X01001QZ1ABCDEFG"]
    assert extract_codes("XABC-DEFG-HIJK-LMNO") == ["XABCDEFGHIJKLMNO"]
    assert extract_codes("PIN: 1234-5678-9999", custom=r"PIN: ([\d-]+)") == ["1234-5678-9999"]
    assert extract_codes("bad regex", custom="(") == []


def test_mask_code():
    assert mask_code("X01001QZ1ABCDEFG") == "X010********DEFG"


def test_parse_expiry():
    assert parse_expiry("12/30") == ("12", "2030")
    assert parse_expiry("07/2031") == ("07", "2031")
    assert parse_expiry("1230") == ("12", "2030")
    assert parse_expiry("13/30") is None
    assert parse_expiry("") is None


def test_parse_cookie():
    assert parse_cookie("Cookie: a=1; b=2=3\n c=4") == {"a": "1", "b": "2=3", "c": "4"}


def test_sanitize_html_removes_card_data():
    html = '<input name="card_number" value="4242 4242 4242 4242"><p>4242424242424242</p><input value=\'123\'>'
    clean = sanitize_html(html)
    assert "4242" not in clean and "123" not in clean
    assert 'value="***"' in clean


def test_load_overrides():
    assert load_overrides("") == ({}, "")
    assert load_overrides('{"buy_now": "#x"}') == ({"buy_now": "#x"}, "")
    assert load_overrides("[1]")[1]
    assert load_overrides("{bad")[1]


def test_playwright_proxy():
    assert playwright_proxy("http://u:p@1.2.3.4:8080") == {"server": "http://1.2.3.4:8080", "username": "u",
                                                           "password": "p"}
    assert playwright_proxy("1.2.3.4:3128") == {"server": "http://1.2.3.4:3128"}
    assert playwright_proxy("") is None


def test_pick_card_from_real_listing_titles():
    cards = [
        {"title": "100 - 2000 TL App Store Card", "inputs": [{"id": "a"}, {"id": "b"}]},
        {"title": "2500 TL App Store Card", "inputs": []},
        {"title": "100 TL App Store Card", "inputs": []},
        {"title": "250 TL App Store Card", "inputs": []},
        {"title": "500 TL App Store Card", "inputs": []},
    ]
    assert pick_card(cards, 100) == (cards[2], False)
    assert pick_card(cards, 250) == (cards[3], False)
    assert pick_card(cards, 2500) == (cards[1], False)
    assert pick_card(cards, 150) == (cards[0], True)
    assert pick_card(cards, 2000) == (cards[0], True)
    assert pick_card(cards, 5000) == (None, False)
    assert pick_card(cards[1:], 150) == (None, False)
    # 自定义金额卡没有输入框时不能用
    assert pick_card([{"title": "100 - 2000 TL App Store Card", "inputs": []}], 150) == (None, False)


def test_money_with_dot_decimals_like_real_site():
    assert find_money("100.00 TL") == [100.0]
    assert find_money("2000.00 TL") == [2000.0]
    assert find_money("1,000.00 TL") == [1000.0]
