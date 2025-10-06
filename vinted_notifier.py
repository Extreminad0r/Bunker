#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vinted_notifier.py
------------------
Integração Apify Actor (opcional) + fallback RSS para obter itens de perfis Vinted
e enviar notificações para um webhook do Discord.

Como funciona:
- Se a variável de ambiente APIFY_TOKEN estiver definida, tenta usar o Actor
  'bebity/vinted-premium-actor' via API Apify (modo "run-sync" / best-effort).
- Se APIFY não estiver disponível ou falhar, usa o feed RSS público:
  https://www.vinted.com/member/<user_id>/items/feed
- Mantém histórico em last_items.json para não reenviar itens já vistos.
- Envia cada novo item como embed ao Discord (título, preço, link, imagem, tamanho).

Variáveis de ambiente:
- DISCORD_WEBHOOK  (obrigatório no GitHub Actions)
- APIFY_TOKEN      (opcional; se fornecido usa Apify Actor para obter dados melhores)
- VINTED_USERS     (opcional; "id1,id2,..."; alternativa ao argumento --users)
- VINTED_BASE_URL  (opcional; ex: https://www.vinted.pt ou https://www.vinted.com)
"""

from __future__ import annotations
import argparse
import json
import os
import re
import sys
import time
from typing import Dict, List, Optional, Tuple, Any

import requests
import xml.etree.ElementTree as ET

# ---------------------------
# Config
# ---------------------------
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)
DEFAULT_BASE_URL = os.getenv("VINTED_BASE_URL", "https://www.vinted.com").rstrip("/")
API_HOST_COM = "https://www.vinted.com"
APIFY_ACTOR_ID = "bebity~vinted-premium-actor"
APIFY_API_BASE = "https://api.apify.com/v2"  # base para chamadas Apify
DEFAULT_PER_PAGE = int(os.getenv("VINTED_PER_PAGE", "20"))
HISTORY_FILE = "last_items.json"
TIMEOUT = 20
RETRY_SLEEP = 0.6

# ---------------------------
# Util / Network client
# ---------------------------
class VintedClient:
    """
    Cliente que primeiro tenta Apify Actor (se APIFY_TOKEN definido), senão
    usa o feed RSS. O método fetch_user_items retorna um dict com chave "items"
    contendo lista de itens normalizados.
    """

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        })
        self.apify_token = os.getenv("APIFY_TOKEN")

    # ---------------------------
    # Apify integration (optional)
    # ---------------------------
    def _call_apify_actor_sync(self, user_id: str, per_page: int = DEFAULT_PER_PAGE, base_url: str = DEFAULT_BASE_URL) -> Optional[List[dict]]:
        """
        Tenta chamar o Actor via endpoint "run-sync-get-dataset-items" (best-effort).
        Se não estiver disponível ou falhar, devolve None.
        """
        if not self.apify_token:
            return None

        # Duas formas possíveis no Apify: run-sync-get-dataset-items (obter dataset direto)
        # ou run-sync. Tentamos ambas (try-except), em ordem.
        payload = {
            # parâmetros que o actor geralmente aceita (variam conforme actor)
            "userId": user_id,
            "domain": base_url.replace("https://", "").replace("http://", ""),
            "limit": per_page,
        }

        # 1) run-sync-get-dataset-items (retorna items no corpo)
        url1 = f"{APIFY_API_BASE}/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items?token={self.apify_token}"
        try:
            r = self.session.post(url1, json=payload, timeout=60)
            if r.status_code == 200:
                data = r.json()
                # data pode ser lista ou object com "items"
                if isinstance(data, list):
                    return data
                if isinstance(data, dict) and "items" in data:
                    return data["items"]
            # se 4xx/5xx: segue para fallback
        except Exception:
            pass

        # 2) run-sync (retorna resultado mais variado; tentamos extrair dataset)
        url2 = f"{APIFY_API_BASE}/acts/{APIFY_ACTOR_ID}/run-sync?token={self.apify_token}"
        try:
            r = self.session.post(url2, json=payload, timeout=60)
            if r.status_code == 200:
                data = r.json()
                # O actor pode devolver dataset.items ou default dataset; normalizamos:
                # Procuramos chaves comuns com items
                if isinstance(data, dict):
                    for key in ("items", "results", "output", "data"):
                        if key in data and isinstance(data[key], list):
                            return data[key]
                    # se o próprio r.json() for lista
                if isinstance(data, list):
                    return data
        except Exception:
            pass

        return None

    # ---------------------------
    # RSS fallback
    # ---------------------------
    def _rss_url(self, user_id: str, base_url: str) -> str:
        # Formato público do feed por utilizador
        return f"{base_url}/member/{user_id}/items/feed"

    def _parse_rss_items(self, rss_text: str) -> List[dict]:
        """Parse básico do RSS e normalização minimalista."""
        try:
            root = ET.fromstring(rss_text)
        except Exception:
            return []

        channel = root.find("channel")
        if channel is None:
            return []

        entries = []
        for node in channel.findall("item"):
            title = (node.findtext("title") or "").strip()
            link = (node.findtext("link") or "").strip()
            desc = node.findtext("description") or ""
            # tenta extrair primeira imagem da descrição HTML
            img = None
            m = re.search(r'img\s+src="([^"]+)"', desc, re.I)
            if m:
                img = m.group(1)

            # tenta extrair id do link (/items/<id>)
            item_id = None
            m2 = re.search(r"/items/(\d+)", link)
            if m2:
                item_id = int(m2.group(1))

            item = {
                "id": item_id if item_id is not None else abs(hash(link)) % (10**9),
                "title": title or (f"Item {item_id}" if item_id else "Item"),
                "url": link or None,
                "photo": {"url": img} if img else {},
            }

            # tenta adivinhar preço por regex no título/descrição
            mprice = re.search(r"(\d+[.,]?\d*)\s?(€|EUR)", title + " " + desc, re.I)
            if mprice:
                item["price"] = f"{mprice.group(1).replace(',', '.')} EUR"

            entries.append(item)
        return entries

    # ---------------------------
    # Public method
    # ---------------------------
    def fetch_user_items(self, user_id: str, per_page: int = DEFAULT_PER_PAGE,
                         base_url: str = DEFAULT_BASE_URL, enrich: bool = True) -> dict:
        """
        Tenta primeiro Apify Actor quando APIFY_TOKEN presente (devolve lista de items normais).
        Se não houver token ou o Actor falhar, usa o feed RSS público.
        """

        # 1) Tenta Apify (se configurado)
        apify_result = None
        if self.apify_token:
            try:
                apify_items = self._call_apify_actor_sync(user_id=user_id, per_page=per_page, base_url=base_url)
                if apify_items:
                    # Normaliza: item pode ter várias chaves; o actor geralmente devolve dicts com campos úteis.
                    normalized = []
                    for it in apify_items:
                        if not isinstance(it, dict):
                            continue
                        # Tentamos extrair id/title/url/photo/price/size
                        item_id = it.get("id") or it.get("item_id") or it.get("itemId") or None
                        try:
                            if isinstance(item_id, str) and item_id.isdigit():
                                item_id = int(item_id)
                        except Exception:
                            pass
                        normalized.append({
                            "id": item_id if item_id is not None else abs(hash(json.dumps(it, sort_keys=True))) % (10**9),
                            "title": it.get("title") or it.get("name") or it.get("subtitle") or "",
                            "url": it.get("url") or it.get("link") or None,
                            "photo": {"url": it.get("image") or it.get("photo") or it.get("image_url") or it.get("thumbnail")} if (it.get("image") or it.get("photo") or it.get("image_url") or it.get("thumbnail")) else {},
                            "price": it.get("price") or it.get("price_text") or it.get("price_str") or None,
                            "raw": it,
                        })
                    apify_result = {"items": normalized}
            except Exception:
                apify_result = None

        if apify_result:
            return apify_result

        # 2) Fallback para RSS público
        rss_url = self._rss_url(user_id, base_url)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
        }
        resp = self.session.get(rss_url, headers=headers, timeout=TIMEOUT)
        resp.raise_for_status()
        items = self._parse_rss_items(resp.text)

        # Enriquecer via /api/v2/items/<id> (best-effort; pode devolver 401/403)
        if enrich:
            enriched = []
            for it in items:
                item_id = it.get("id")
                if item_id and isinstance(item_id, int):
                    try:
                        r2 = self.session.get(f"{API_HOST_COM}/api/v2/items/{item_id}", timeout=TIMEOUT)
                        if r2.status_code == 200:
                            d = r2.json()
                            # Vinted pode usar {"item": {...}} ou retornar o objecto direto
                            source = d.get("item") if isinstance(d, dict) and "item" in d else d
                            if isinstance(source, dict):
                                # Preenchimentos se ausentes
                                if not it.get("price"):
                                    if isinstance(source.get("price"), str) and source.get("price").strip():
                                        it["price"] = source.get("price").strip()
                                    elif source.get("price_numeric") and source.get("currency"):
                                        try:
                                            it["price"] = f"{float(source['price_numeric']):.2f} {source['currency']}"
                                        except Exception:
                                            pass
                                # imagem
                                photo = None
                                if isinstance(source.get("photo"), dict) and source["photo"].get("url"):
                                    photo = source["photo"]["url"]
                                photos = source.get("photos") or []
                                if not photo and isinstance(photos, list) and photos:
                                    if isinstance(photos[0], dict) and photos[0].get("url"):
                                        photo = photos[0]["url"]
                                if photo:
                                    it["photo"] = {"url": photo}
                                # tamanho
                                for key in ("size_title", "size_label", "size_text", "brand_size", "size"):
                                    if isinstance(source.get(key), str) and source.get(key).strip():
                                        it[key] = source.get(key).strip()
                                        break
                    except Exception:
                        # ignorar erros e manter dados do RSS
                        pass
                enriched.append(it)
            items = enriched

        return {"items": items}

# ---------------------------
# History and helpers
# ---------------------------
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
    # tentativa a partir de raw
    raw = item.get("raw") or {}
    if isinstance(raw, dict):
        for k in ("image", "image_url", "thumbnail", "photo"):
            if raw.get(k):
                return raw.get(k)
    return None

def item_size(item: dict) -> Optional[str]:
    for key in ("size_title", "size_label", "size_text", "brand_size", "size"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    raw = item.get("raw") or {}
    if isinstance(raw, dict):
        for k in ("size_title", "size_label", "size"):
            if raw.get(k):
                return raw.get(k)
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
        time.sleep(0.35)
    return ok_all, msg

def parse_user_ids(cli_users: Optional[str]) -> List[str]:
    raw = cli_users or os.getenv("VINTED_USERS", "")
    ids = [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
    return [x for x in ids if x.isdigit()]

# ---------------------------
# Main
# ---------------------------
def main():
    parser = argparse.ArgumentParser(description="Vinted → Discord Notifier (Apify + RSS fallback)")
    parser.add_argument("--users", help="IDs de utilizador da Vinted separados por vírgula. Ex: 278727725,123456")
    parser.add_argument("--webhook", help="URL do webhook do Discord (ou env DISCORD_WEBHOOK).")
    parser.add_argument("--per-page", type=int, default=DEFAULT_PER_PAGE, help="Número de itens (padrão).")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Domínio base (ex: https://www.vinted.pt ou https://www.vinted.com).")
    args = parser.parse_args()

    webhook_url = args.webhook or os.getenv("DISCORD_WEBHOOK")
    if not webhook_url:
        print("Erro: precisa fornecer o webhook do Discord via --webhook ou env DISCORD_WEBHOOK.", file=sys.stderr)
        sys.exit(2)

    user_ids = parse_user_ids(args.users)
    if not user_ids:
        user_ids = ["278727725"]
        print("Aviso: nenhum --users/env VINTED_USERS fornecido. A usar o exemplo 278727725.")

    history = load_history(HISTORY_FILE)
    client = VintedClient()

    total_new = 0
    all_embeds: List[dict] = []

    for user_id in user_ids:
        print(f"[Vinted] A verificar utilizador {user_id} ...")
        try:
            data = client.fetch_user_items(user_id=user_id, per_page=args.per_page, base_url=args.base_url)
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
            try:
                if isinstance(it_id, str) and it_id.isdigit():
                    it_id = int(it_id)
            except Exception:
                pass
            if isinstance(it_id, int) and it_id not in known_ids:
                new_items.append(it)

        new_items_sorted = sorted(new_items, key=lambda x: x.get("id", 0))
        print(f"  - Encontrados {len(new_items_sorted)} novos itens para user {user_id}.")
        total_new += len(new_items_sorted)

        for it in new_items_sorted:
            it_id = it.get("id")
            try:
                if isinstance(it_id, str) and it_id.isdigit():
                    it_id = int(it_id)
            except Exception:
                pass
            if isinstance(it_id, int):
                known_ids.add(it_id)

        history[user_id] = sorted(list(known_ids), reverse=True)[:200]

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
