#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vinted Notifier (versão RSS estável, sem token/cookies)
-------------------------------------------------------
- NÃO visita homepage para apanhar CSRF.
- NÃO usa o antigo /api/v2/token (depreciado).
- Vai ao feed RSS público do perfil: https://www.vinted.{tld}/member/<user_id>/items/feed
- Tenta, opcionalmente, enriquecer com detalhes via /api/v2/items/<id> (se acessível; ignora erros 401/403).
- Detecta apenas novos artigos (IDs), guarda last_items.json, envia embeds para Discord.

Uso:
  python vinted_notifier.py --users 278727725 --webhook $DISCORD_WEBHOOK
Variáveis:
  DISCORD_WEBHOOK  (obrigatória no GitHub Actions)
  VINTED_USERS     (ex: "278727725,123456")
  VINTED_BASE_URL  (padrão https://www.vinted.com; para .pt usa https://www.vinted.pt)
  VINTED_PER_PAGE  (sem efeito no RSS; mantido por compatibilidade)
"""

import argparse
import json
import os
import re
import sys
import time
from typing import Dict, List, Optional, Tuple
import requests
import xml.etree.ElementTree as ET

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

# Para o RSS e construção de links
DEFAULT_BASE_URL = os.getenv("VINTED_BASE_URL", "https://www.vinted.com").rstrip("/")

# Endpoint opcional de detalhe por item (pode falhar sem sessão; usamos best-effort)
API_HOST_COM = "https://www.vinted.com"

DEFAULT_PER_PAGE = int(os.getenv("VINTED_PER_PAGE", "20"))
HISTORY_FILE = "last_items.json"
TIMEOUT = 15
RETRY_SLEEP = 0.6


class VintedClient:
    """Cliente minimalista para o feed RSS + enriquecimento best-effort."""

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        })

    def _rss_url(self, user_id: str, base_url: str) -> str:
        return f"{base_url}/member/{user_id}/items/feed"

    def fetch_user_items(
        self,
        user_id: str,
        per_page: int = DEFAULT_PER_PAGE,  # ignorado no RSS; mantido para compat.
        page: int = 1,                     # idem
        order: str = "newest_first",       # idem
        base_url: str = DEFAULT_BASE_URL,
        enrich: bool = True,
    ) -> dict:
        """
        Lê o feed RSS do perfil e devolve {"items": [ ... ]} com campos usados no resto do script.
        Se enrich=True, tenta chamar /api/v2/items/<id> para obter preço, tamanho, imagens melhores.
        """
        rss_url = self._rss_url(user_id, base_url)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
        }
        resp = self.session.get(rss_url, headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()

        root = ET.fromstring(resp.text)
        channel = root.find("channel")
        if channel is None:
            return {"items": []}

        items_out = []
        for node in channel.findall("item"):
            title = (node.findtext("title") or "").strip()
            link = (node.findtext("link") or "").strip()
            desc = node.findtext("description") or ""
            # tenta extrair URL de imagem do HTML dentro da descrição
            img = None
            m = re.search(r'img\s+src="([^"]+)"', desc)
            if m:
                img = m.group(1)

            # tenta extrair id do link /items/<id>
            item_id = None
            m2 = re.search(r"/items/(\d+)", link)
            if m2:
                item_id = int(m2.group(1))

            # prepara estrutura base
            item_obj = {
                "id": item_id if item_id is not None else abs(hash(link)) % (10**9),
                "title": title or (f"Item {item_id}" if item_id else "Item"),
                "url": link or None,
                "photo": {"url": img} if img else {},
                # preço e tamanho tentaremos inferir/enriquecer
            }

            # tenta apanhar preço do título/descrição (nem sempre presente no RSS)
            price_guess = None
            mprice = re.search(r"(\d+[.,]?\d*)\s?(€|EUR)", title + " " + desc, re.I)
            if mprice:
                price_guess = f"{mprice.group(1).replace(',', '.')} EUR"
                item_obj["price"] = price_guess

            # Enriquecimento best-effort via /api/v2/items/<id>
            if enrich and item_id:
                try:
                    url = f"{API_HOST_COM}/api/v2/items/{item_id}"
                    r2 = self.session.get(url, timeout=TIMEOUT)
                    if r2.status_code == 200:
                        data = r2.json()
                        d = data.get("item") or data
                        if isinstance(d, dict):
                            # preço
                            if "price" in d and isinstance(d["price"], str) and d["price"].strip():
                                item_obj["price"] = d["price"].strip()
                            elif d.get("price_numeric") and d.get("currency"):
                                try:
                                    item_obj["price"] = f"{float(d['price_numeric']):.2f} {d['currency']}"
                                except Exception:
                                    pass
                            # tamanho
                            for key in ("size_title", "size_label", "size_text", "brand_size"):
                                if isinstance(d.get(key), str) and d[key].strip():
                                    item_obj[key] = d[key].strip()
                                    break
                            # imagem primária
                            photo_url = None
                            if isinstance(d.get("photo"), dict) and d["photo"].get("url"):
                                photo_url = d["photo"]["url"]
                            photos = d.get("photos") or []
                            if not photo_url and isinstance(photos, list) and photos:
                                if isinstance(photos[0], dict) and photos[0].get("url"):
                                    photo_url = photos[0]["url"]
                            if photo_url:
                                item_obj["photo"] = {"url": photo_url}
                    # Se 401/403/404, ignoramos e seguimos com dados do RSS
                except Exception:
                    pass

            items_out.append(item_obj)

        return {"items": items_out}


def load_history(path: str = HISTORY_FILE) -> Dict[str, List[int]]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {str(k): list(map(int, v)) for k, v in data.items()}
    except Exception:
        return {}


def save_history(history: Dict[str, List[int]], path: str = HISTORY_FILE) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def item_primary_image(item: dict) -> Optional[str]:
    for key in ("photo", "image"):
        node = item.get(key)
        if isinstance(node, dict) and node.get("url"):
            return node["url"]
    photos = item.get("photos") or item.get("images") or []
    if isinstance(photos, list) and photos:
        if isinstance(photos[0], dict) and photos[0].get("url"):
            return photos[0]["url"]
    return None


def item_size(item: dict) -> Optional[str]:
    for key in ("size_title", "size_label", "size_text", "brand_size", "size"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    node = item.get("size")
    if isinstance(node, dict):
        for key in ("title", "label", "name"):
            if node.get(key):
                return str(node[key])
    return None


def item_price_text(item: dict) -> str:
    if isinstance(item.get("price"), str) and item["price"].strip():
        return item["price"].strip()
    amount = item.get("price_numeric") or item.get("price_amount") or item.get("amount")
    currency = item.get("currency") or item.get("currency_code") or item.get("price_currency") or "EUR"
    if amount is not None:
        try:
            value = float(str(amount).replace(",", "."))
            return f"{value:.2f} {currency}"
        except Exception:
            return f"{amount} {currency}".strip()
    return "Preço não disponível"


def item_url(item: dict, base: str = DEFAULT_BASE_URL) -> Optional[str]:
    if isinstance(item.get("url"), str) and item["url"].startswith("/"):
        return base.rstrip("/") + item["url"]
    if isinstance(item.get("url"), str) and item["url"].startswith("http"):
        return item["url"]
    item_id = item.get("id")
    if item_id:
        return f"{base.rstrip('/')}/items/{item_id}"
    return None


def build_discord_embed(item: dict, base_url: str = DEFAULT_BASE_URL) -> dict:
    title = item.get("title") or item.get("name") or f"Item #{item.get('id')}"
    url = item_url(item, base_url) or base_url
    price = item_price_text(item)
    size_txt = item_size(item)
    description_lines = [f"**Preço:** {price}"]
    if size_txt:
        description_lines.append(f"**Tamanho:** {size_txt}")
    description = "\n".join(description_lines)

    image_url = item_primary_image(item)
    embed = {
        "title": str(title)[:256],
        "url": url,
        "description": description[:2048],
    }
    if image_url:
        embed["image"] = {"url": image_url}

    fields = []
    if size_txt:
        fields.append({"name": "Tamanho", "value": size_txt, "inline": True})
    if price and price != "Preço não disponível":
        fields.append({"name": "Preço", "value": price, "inline": True})
    if fields:
        embed["fields"] = fields
    return embed


def post_to_discord(webhook_url: str, embeds: List[dict]) -> Tuple[bool, str]:
    ok_all = True
    msg = ""
    CHUNK = 10
    for i in range(0, len(embeds), CHUNK):
        payload = {"embeds": embeds[i:i + CHUNK]}
        try:
            resp = requests.post(webhook_url, json=payload, timeout=TIMEOUT)
            if not (200 <= resp.status_code < 300):
                ok_all = False
                msg = f"Falha do Discord ({resp.status_code}): {resp.text[:300]}"
        except Exception as e:
            ok_all = False
            msg = f"Erro ao enviar para Discord: {e}"
        time.sleep(0.4)
    return ok_all, msg


def parse_user_ids(cli_users: Optional[str]) -> List[str]:
    raw = cli_users or os.getenv("VINTED_USERS", "")
    ids = [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
    return [x for x in ids if x.isdigit()]


def main():
    parser = argparse.ArgumentParser(description="Vinted → Discord Notifier (RSS estável)")
    parser.add_argument("--users", help="IDs de utilizador da Vinted separados por vírgula. Ex: 278727725,123456")
    parser.add_argument("--webhook", help="URL do webhook do Discord (ou env DISCORD_WEBHOOK).")
    parser.add_argument("--per-page", type=int, default=DEFAULT_PER_PAGE, help="Compatibilidade; ignorado no RSS.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"Domínio base (padrão {DEFAULT_BASE_URL}).")
    args = parser.parse_args()

    webhook_url = args.webhook or os.getenv("DISCORD_WEBHOOK")
    if not webhook_url:
        print("Erro: precisa fornecer o webhook do Discord via --webhook ou env DISCORD_WEBHOOK.", file=sys.stderr)
        sys.exit(2)

    user_ids = parse_user_ids(args.users)
    if not user_ids:
        user_ids = ["278727725"]
        print("Aviso: nenhum --users/env VINTED_USERS fornecido. "
              "A usar o exemplo 278727725 (https://www.vinted.pt/member/278727725).")

    history = load_history(HISTORY_FILE)
    client = VintedClient()

    total_new = 0
    all_embeds: List[dict] = []

    for user_id in user_ids:
        print(f"[Vinted] A verificar utilizador {user_id} ...")
        try:
            data = client.fetch_user_items(user_id=user_id, base_url=args.base_url)
        except requests.HTTPError as e:
            print(f"  - Falha HTTP para user {user_id}: {e}", file=sys.stderr)
            continue
        except Exception as e:
            print(f"  - Erro ao obter itens para user {user_id}: {e}", file=sys.stderr)
            continue

        items = data.get("items") or []
        if not isinstance(items, list):
            print(f"  - Resposta inesperada para user {user_id}: sem lista de itens.", file=sys.stderr)
            continue

        known_ids = set(history.get(user_id, []))
        new_items = []
        for it in items:
            it_id = it.get("id")
            if isinstance(it_id, str) and it_id.isdigit():
                it_id = int(it_id)
            if isinstance(it_id, int) and it_id not in known_ids:
                new_items.append(it)

        new_items_sorted = sorted(new_items, key=lambda x: x.get("id", 0))
        print(f"  - Encontrados {len(new_items_sorted)} novos itens para user {user_id}.")
        total_new += len(new_items_sorted)

        for it in new_items_sorted:
            it_id = it.get("id")
            if isinstance(it_id, str) and it_id.isdigit():
                it_id = int(it_id)
            if isinstance(it_id, int):
                known_ids.add(it_id)

        trimmed = sorted(list(known_ids), reverse=True)[:200]
        history[user_id] = trimmed

        for it in new_items_sorted:
            embed = build_discord_embed(it, base_url=args.base_url)
            all_embeds.append(embed)

    try:
        save_history(history, HISTORY_FILE)
    except Exception as e:
        print(f"Aviso: não consegui guardar {HISTORY_FILE}: {e}", file=sys.stderr)

    if all_embeds:
        ok, msg = post_to_discord(webhook_url, all_embeds)
        if ok:
            print(f"[Discord] Enviados {len(all_embeds)} embed(s) com sucesso.")
        else:
            print(f"[Discord] Alguns envios falharam: {msg}", file=sys.stderr)
    else:
        print("[Vinted] Sem novos itens para enviar.")

    print(f"[Resumo] Novos itens encontrados: {total_new}.")


if __name__ == "__main__":
    main()
