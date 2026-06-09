"""Юнит-тесты парсера внешних подписок (чистые функции, без БД/сети)."""

import base64

from app.services import external_subscription_service as svc


PLAIN = (
    'vless://uuid-1@host1.example.com:443?type=tcp#Server%20One\n'
    'trojan://pass@host2.example.com:8443#Server Two\n'
    'not-a-link\n'
    'ss://YWVzLTI1Ni1nY206cGFzcw==@host3.example.com:8388#SS Node\n'
)


def test_extract_plaintext_links():
    links = svc.parse_subscription(PLAIN)
    assert len(links) == 3
    assert links[0]['protocol'] == 'vless'
    assert links[0]['name'] == 'Server One'  # url-decoded fragment
    assert links[1]['protocol'] == 'trojan'
    assert links[2]['protocol'] == 'ss'


def test_parse_base64_fallback():
    encoded = base64.b64encode(PLAIN.encode('utf-8')).decode('utf-8')
    links = svc.parse_subscription(encoded)
    assert len(links) == 3
    assert {ln['protocol'] for ln in links} == {'vless', 'trojan', 'ss'}


def test_parse_empty_and_garbage():
    assert svc.parse_subscription('') == []
    assert svc.parse_subscription('just some text\nno links here') == []


def test_protocol_detection():
    assert svc._protocol_of('vmess://abc') == 'vmess'
    assert svc._protocol_of('hy2://abc') == 'hy2'
    assert svc._protocol_of('http://abc') is None


def test_remote_key_stable_across_param_change():
    # Та же нода (host:port) с разными query-параметрами и тем же именем → одинаковый remote_key
    link_a = 'vless://uuid@host.example.com:443?sni=a.com#NodeA'
    link_b = 'vless://uuid@host.example.com:443?sni=b.com#NodeA'
    key_a = svc._remote_key(link_a, 'vless', 'NodeA')
    key_b = svc._remote_key(link_b, 'vless', 'NodeB')  # different name
    key_a2 = svc._remote_key(link_b, 'vless', 'NodeA')  # same name, changed params
    assert key_a == key_a2  # имя+host:port стабильны → один ключ
    assert key_a != key_b  # другое имя → другой ключ
    assert 'host.example.com:443' in key_a


def test_remote_key_truncated_to_255():
    long_name = 'X' * 400
    key = svc._remote_key('vless://uuid@h.com:443#x', 'vless', long_name)
    assert len(key) <= 255


def test_build_links_join_then_b64_roundtrip():
    # Эмулируем итоговую сборку: base64 от \n-списка
    links = ['vless://a', 'trojan://b']
    body = base64.b64encode('\n'.join(links).encode()).decode()
    decoded = base64.b64decode(body).decode()
    assert decoded.splitlines() == links


def test_merge_remnawave_base64_with_external():
    # Remnawave отдаёт base64-список → внешние добавляются в конец, всё перекодируется
    rw_links = ['vless://uuid@panel.example.com:443#Panel-1', 'vless://uuid@panel2.example.com:443#Panel-2']
    rw_body = base64.b64encode('\n'.join(rw_links).encode()).decode()
    external = ['trojan://pass@ext.example.com:8443#Ext-1']
    merged = svc.merge_remnawave_with_external(rw_body, external)
    decoded = base64.b64decode(merged).decode().splitlines()
    assert decoded == rw_links + external  # порядок: сначала панель, затем внешние


def test_merge_remnawave_plaintext_with_external():
    rw_body = 'vless://uuid@panel.example.com:443#Panel-1\n'
    merged = svc.merge_remnawave_with_external(rw_body, ['ss://x@e.com:8388#E'])
    decoded = base64.b64decode(merged).decode().splitlines()
    assert decoded == ['vless://uuid@panel.example.com:443#Panel-1', 'ss://x@e.com:8388#E']


def test_merge_passthrough_for_non_link_body():
    # Не список ссылок (напр. clash YAML) → отдаём как есть, внешние не подмешиваем
    yaml_body = 'proxies:\n  - name: a\n    type: vless\n'
    merged = svc.merge_remnawave_with_external(yaml_body, ['vless://ext'])
    assert merged == yaml_body


def test_merge_no_external_keeps_panel_only():
    rw_links = ['vless://uuid@p.example.com:443#P']
    rw_body = base64.b64encode('\n'.join(rw_links).encode()).decode()
    merged = svc.merge_remnawave_with_external(rw_body, [])
    assert base64.b64decode(merged).decode().splitlines() == rw_links


def test_apply_display_name_replaces_fragment():
    out = svc.apply_display_name('vless://uuid@h.com:443?x=1#OldName', 'Моя Нода')
    base, frag = out.rsplit('#', 1)
    assert base == 'vless://uuid@h.com:443?x=1'
    assert frag == '%D0%9C%D0%BE%D1%8F%20%D0%9D%D0%BE%D0%B4%D0%B0'  # url-encoded


def test_apply_display_name_adds_fragment_when_absent():
    out = svc.apply_display_name('trojan://p@h.com:8443', 'Name')
    assert out == 'trojan://p@h.com:8443#Name'


def test_apply_display_name_empty_keeps_original():
    raw = 'vless://uuid@h.com:443#Orig'
    assert svc.apply_display_name(raw, '') == raw
    assert svc.apply_display_name(raw, None) == raw


def test_apply_display_name_skips_vmess():
    raw = 'vmess://eyJhZGQiOiJoLmNvbSJ9'
    assert svc.apply_display_name(raw, 'Custom') == raw  # vmess не трогаем
