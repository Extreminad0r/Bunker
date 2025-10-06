#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vinted Notifier
---------------
Verifica novos artigos publicados em um ou mais perfis da Vinted e envia notificações
para um webhook do Discord como embeds.

Características:
- NÃO visita homepage, NÃO precisa de CSRF/cookies.
- Obtém guest token via GET https://www.vinted.com/api/v2/token (User-Agent + Accept).
- Usa Authorization: Bearer <token> nas chamadas seguintes.
- Lê itens em https://www.vinted.com/api/v2/users/<user_id>/items
- Detecta apenas artigos novos (compara IDs) e guarda histórico em last_items.json.
- Revalida token automaticamente se receber 401.
- Suporta múltiplos perfis (lista de IDs por argumento/env).
- Envia cada novo item como embed (título, preço, link, imagem, tamanho quando disponível).

Uso:
    python vinted_notifier.py --users 278727725,123456789 --webhook $DISCORD_WEBHOOK
Variáveis de ambiente:
    DISCORD_WEBHOOK  (obrigatória no GitHub Actions; localmente pode ser usada)
    VINTED_USERS     (opcional: "id1,id2,..."; alternativa ao --users)
    VINTED_PER_PAGE  (opcional: nº de itens por chamada; padrão 20)
    VINTED_BASE_URL  (opcional: base para construir links, padrão https://www.vinted.com)
Arquivos:
    last_items.json  (criado/atualizado no diretório atual)

Autor: você 😉
"""

import argparse
import json
import os
import sys
import time
from typing import Dict, List, Optional, Tuple
import requests

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)
API_HOST = "https://www.vinted.com"  # API é .com mesmo que o front seja .pt
TOKEN_ENDPOINT = f"{API_HOST}/api/v2/token"
USER_ITEMS_ENDPOINT = f"{API_HOST}/api/v2/users/{{user_id}}/items"

DEFAULT_PER_PAGE = int(os.getenv("VINTED_PER_PAGE", "20"))
DEFAULT_BASE_URL = os.getenv("VINTED_BASE_URL", "https://www.vinted.com")  # usado para links do item

HISTORY_FILE = "last_items.json"
TIMEOUT = 15  # segundos
RETRY_SLEEP = 1.2  # segundos entre tentativas leves


class VintedClient:
    """Cliente minimalista da API pública da Vinted com token convidado (guest)."""

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        })
        self._token: Optional[str] = None

    def _ensure_token(self) -> None:
        """Obtém (ou renova) o guest token via /api/v2/token."""
        resp = self.session.get(TOKEN_ENDPOINT, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        token = data.get("token") or data.get("access_token") or data.get("guest_token")
        if not token:
            raise RuntimeError("Não foi possível obter token convidado da Vinted.")
        self._token = token
        # Atualiza o header Authorization
        self.session.headers.update({"Authorization": f"Bearer {self._token}"})

    def _authorized_get(self, url: str, params: Optional[dict] = None) -> requests.Response:
        """
        GET autenticado com Bearer. Se 401, renova token e repete uma vez.
        """
        if not self._token:
            self._ensure_token()
        resp = self.session.get(url, params=params, timeout=TIMEOUT)
        if resp.status_code == 401:
            # token expirou/invalidado -> renova e tenta de novo
            self._ensure_token()
            time.sleep(RETRY_SLEEP)
            resp = self.session.get(url, params=params, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp

    def fetch_user_items(
        self,
        user_id: str,
        per_page: int = DEFAULT_PER_PAGE,
        page: int = 1,
        order: str = "newest_first",
        status: str = "active",
    ) -> dict:
        """
        Obtém itens de um utilizador. Parâmetros comuns:
          - per_page: 20 recomendado (a API geralmente suporta 100, mas 20 é seguro)
          - page: página (1-based)
          - order: 'newest_first' para ver os mais recentes primeiro
          - status: 'active' para itens ativos
        """
        params = {
            "page": page,
            "per_page": per_page,
            "order": order,
            "status": status,
        }
        url = USER_ITEMS_ENDPOINT.format(user_id=user_id)
        resp = self._authorized_get(url, params=params)
        return resp.json()


def load_history(path: str = HISTORY_FILE) -> Dict[str, List[int]]:
    """Carrega histórico de IDs por user_id. Estrutura: { "<user_id>": [id1, id2, ...] }"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            # normalização básica
            return {str(k): list(map(int, v)) for k, v in data.items()}
    except Exception:
        # Se algo correr mal, não bloqueia
        return {}


def save_history(history: Dict[str, List[int]], path: str = HISTORY_FILE) -> None:
    """Guarda histórico em disco."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def item_primary_image(item: dict) -> Optional[str]:
    """Extrai URL da imagem principal, se existir, com fallback robusto."""
    # Estruturas comuns na Vinted:
    # item["photo"]["url"], item["photos"][0]["url"], ou item["image"]["url"]
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
    """Tenta obter o tamanho (size) quando disponível."""
    # Várias formas possíveis: "size", "size_title", "size_label", "size_text", "brand_size"
    for key in ("size_title", "size_label", "size_text", "brand_size", "size"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # Algumas vezes vem dentro de "size" como objeto
    node = item.get("size")
    if isinstance(node, dict):
        for key in ("title", "label", "name"):
            if node.get(key):
                return str(node[key])
    return None


def item_price_text(item: dict) -> str:
    """Forma uma string de preço amigável, lidando com chaves diferentes."""
    # Possibilidades: "price" (string já formatada), "price_numeric"/"price_amount" + "currency"
    if isinstance(item.get("price"), str) and item["price"].strip():
        return item["price"].strip()
    amount = item.get("price_numeric") or item.get("price_amount") or item.get("amount") or item.get("total_item_price")
    currency = item.get("currency") or item.get("currency_code") or item.get("price_currency")
    if amount is not None and currency:
        try:
            # Alguns endpoints retornam amount como string/numérico
            value = float(amount)
            return f"{value:.2f} {currency}"
        except Exception:
            return f"{amount} {currency}".strip()
    # Fallback final
    return "Preço não disponível"


def item_url(item: dict, base: str = DEFAULT_BASE_URL) -> Optional[str]:
    """Constroi URL do item, usando 'url' relativo ou pelo id."""
    if isinstance(item.get("url"), str) and item["url"].startswith("/"):
        return base.rstrip("/") + item["url"]
    if isinstance(item.get("url"), str) and item["url"].startswith("http"):
        return item["url"]
    # Fallback pelo id (formato clássico /items/<id>)
    item_id = item.get("id")
    if item_id:
        return f"{base.rstrip('/')}/items/{item_id}"
    return None


def build_discord_embed(item: dict, base_url: str = DEFAULT_BASE_URL) -> dict:
    """Monta um embed do Discord para um item Vinted."""
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
    # Campos extra (opcional)
    fields = []
    if size_txt:
        fields.append({"name": "Tamanho", "value": size_txt, "inline": True})
    if price and price != "Preço não disponível":
        fields.append({"name": "Preço", "value": price, "inline": True})
    if fields:
        embed["fields"] = fields
    return embed


def post_to_discord(webhook_url: str, embeds: List[dict]) -> Tuple[bool, str]:
    """Envia uma lista de embeds ao webhook do Discord (máx. 10 por payload)."""
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
                # Continua a tentar enviar os próximos para não perder tudo
        except Exception as e:
            ok_all = False
            msg = f"Erro ao enviar para Discord: {e}"
        time.sleep(0.4)  # leve intervalo para respeitar rate limits
    return ok_all, msg


def parse_user_ids(cli_users: Optional[str]) -> List[str]:
    """Lê user IDs a partir de --users ou env VINTED_USERS."""
    raw = cli_users or os.getenv("VINTED_USERS", "")
    ids = [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
    # Validação leve: só números
    only_digits = [x for x in ids if x.isdigit()]
    return only_digits


def main():
    parser = argparse.ArgumentParser(description="Vinted → Discord Notifier (guest token)")
    parser.add_argument(
        "--users",
        help="Lista de IDs de utilizador da Vinted separados por vírgula. Ex: 278727725,123456",
    )
    parser.add_argument(
        "--webhook",
        help="URL do webhook do Discord (pode usar env DISCORD_WEBHOOK).",
    )
    parser.add_argument(
        "--per-page",
        type=int,
        default=DEFAULT_PER_PAGE,
        help=f"Itens por chamada (padrão {DEFAULT_PER_PAGE}).",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Base para montar links dos itens (padrão {DEFAULT_BASE_URL}).",
    )
    args = parser.parse_args()

    webhook_url = args.webhook or os.getenv("DISCORD_WEBHOOK")
    if not webhook_url:
        print("Erro: precisa fornecer o webhook do Discord via --webhook ou env DISCORD_WEBHOOK.", file=sys.stderr)
        sys.exit(2)

    user_ids = parse_user_ids(args.users)
    if not user_ids:
        # Exemplo mínimo: ID do perfil fornecido no enunciado
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
            data = client.fetch_user_items(user_id=user_id, per_page=args.per_page)
        except requests.HTTPError as e:
            print(f"  - Falha HTTP para user {user_id}: {e}", file=sys.stderr)
            continue
        except Exception as e:
            print(f"  - Erro ao obter itens para user {user_id}: {e}", file=sys.stderr)
            continue

        items = data.get("items") or data.get("catalog_items") or data.get("result") or []
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

        # Ordena do mais antigo para o mais recente para que as mensagens no Discord
        # apareçam em ordem cronológica crescente (opcional, mas agradável).
        new_items_sorted = sorted(new_items, key=lambda x: x.get("id", 0))

        print(f"  - Encontrados {len(new_items_sorted)} novos itens para user {user_id}.")
        total_new += len(new_items_sorted)

        # Atualiza histórico com os IDs novos + mantém um limite razoável
        for it in new_items_sorted:
            it_id = it.get("id")
            if isinstance(it_id, str) and it_id.isdigit():
                it_id = int(it_id)
            if isinstance(it_id, int):
                known_ids.add(it_id)

        # Mantém os últimos 200 IDs por utilizador (para não crescer infinito)
        trimmed = sorted(list(known_ids), reverse=True)[:200]
        history[user_id] = trimmed

        # Prepara embeds para o Discord
        for it in new_items_sorted:
            embed = build_discord_embed(it, base_url=args.base_url)
            all_embeds.append(embed)

    # Persiste histórico ANTES de enviar (para evitar duplicados em caso de falha posterior)
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
