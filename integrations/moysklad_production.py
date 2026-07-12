"""МойСклад API клиент для производственных заданий: поиск товаров с техкартами,
остатки на складе Производство и создание ПЗ."""

import ssl
from collections import defaultdict
from typing import Optional

import aiohttp

from core import config
from core.logger import logger
from services.production_excluded import EXCLUDED_PRODUCT_CODES


def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _headers() -> dict:
    return {
        'Authorization': f'Bearer {config.MOYSKLAD_TOKEN}',
        'Content-Type': 'application/json',
        'Accept-Encoding': 'gzip',
    }


def _meta(entity_type: str, entity_id: str) -> dict:
    return {
        'meta': {
            'href': f'{config.MOYSKLAD_BASE_URL}/entity/{entity_type}/{entity_id}',
            'type': entity_type,
            'mediaType': 'application/json',
        }
    }


def _href_id(href: str) -> str:
    return href.split('/')[-1].split('?')[0]


_STOPWORDS = frozenset({
    'для', 'в', 'на', 'к', 'по', 'с', 'со', 'у', 'от', 'до', 'из',
    'при', 'без', 'над', 'под', 'про', 'за', 'и', 'а', 'но', 'или',
    'мл', 'г', 'кг', 'л', 'шт', 'мм', 'см', 'м',
})


def _word_stems(query: str, min_len: int = 5) -> list[str]:
    """Превратить «основа для шампуня» в ['основ', 'шампу'] — корни ≥5 букв, без стоп-слов.

    Возвращает [] если ни одно слово не подходит — это сигнал «слишком общий запрос»,
    локальный фильтр пропускается."""
    out = []
    for w in query.lower().replace(',', ' ').replace('.', ' ').split():
        if w in _STOPWORDS or len(w) < min_len:
            continue
        out.append(w[:min_len])
    return out


class MoySkladProductionClient:
    """Все методы async. Один клиент на ProductionTaskService."""

    def __init__(self):
        self.base = config.MOYSKLAD_BASE_URL

    async def _get(self, session: aiohttp.ClientSession, path: str, params: Optional[dict] = None) -> dict:
        url = path if path.startswith('http') else f'{self.base}{path}'
        async with session.get(url, headers=_headers(), params=params, ssl=_ssl_ctx()) as r:
            if r.status != 200:
                text = await r.text()
                raise RuntimeError(f'GET {url} → {r.status}: {text[:300]}')
            return await r.json()

    async def _post(self, session: aiohttp.ClientSession, path: str, payload: dict) -> dict:
        url = path if path.startswith('http') else f'{self.base}{path}'
        async with session.post(url, headers=_headers(), json=payload, ssl=_ssl_ctx()) as r:
            if r.status not in (200, 201):
                text = await r.text()
                raise RuntimeError(f'POST {url} → {r.status}: {text[:300]}')
            return await r.json()

    async def build_plans_map(self) -> dict[str, dict]:
        """Маппинг {код товара: {plan_id, product_name}} — какие товары имеют техкарту.
        Старые позиции из EXCLUDED_PRODUCT_CODES исключаются."""
        out: dict[str, dict] = {}
        offset = 0
        async with aiohttp.ClientSession() as s:
            while True:
                data = await self._get(s, '/entity/processingplan', {
                    'limit': 100, 'offset': offset, 'expand': 'products.assortment',
                })
                rows = data.get('rows', [])
                for plan in rows:
                    for prod in plan.get('products', {}).get('rows', []):
                        a = prod.get('assortment', {}) or {}
                        code = a.get('code', '')
                        if code and code not in EXCLUDED_PRODUCT_CODES:
                            out[code] = {
                                'plan_id': plan['id'],
                                'product_name': a.get('name', ''),
                                'product_id': a.get('id', ''),
                            }
                if len(rows) < 100:
                    break
                offset += 100
        return out

    async def search_products_with_plans(self, query: str, limit: int = 30) -> list[dict]:
        """Найти товары по строке, отфильтровать: (1) только с техкартой, (2) не из exclude-листа.

        Двухпроходный поиск чтобы обойти склонения и предлоги в русском:
          (a) МС-search — точный, но требует совпадения подстроки
          (b) локальный фильтр build_plans_map по корням слов (≥5 букв), без стоп-слов

        Результат — объединение по коду. Возвращает [{code, name, plan_id, is_base}].
        """
        plans = await self.build_plans_map()

        # (a) МС-search
        async with aiohttp.ClientSession() as s:
            data = await self._get(s, '/entity/product', {
                'search': query, 'limit': limit,
            })

        found_codes: dict[str, str] = {}  # code -> name (из ответа МС, актуальное имя)
        for p in data.get('rows', []):
            code = p.get('code', '')
            if code and code in plans and code not in EXCLUDED_PRODUCT_CODES:
                found_codes[code] = p.get('name', '')

        # (b) локальный fallback — корни слов в названиях техкарт-продуктов
        stems = _word_stems(query)
        if stems:
            for code, info in plans.items():
                if code in found_codes or code in EXCLUDED_PRODUCT_CODES:
                    continue
                name_l = (info['product_name'] or '').lower()
                if all(s in name_l for s in stems):
                    found_codes[code] = info['product_name']

        out = []
        for code, name in found_codes.items():
            out.append({
                'code': code,
                'name': name,
                'plan_id': plans[code]['plan_id'],
                'is_base': 'Основа' in name,
            })
        out.sort(key=lambda x: x['code'])
        return out

    async def stock_at_production(self) -> dict[str, float]:
        """Остатки на складе Производство: {product_id: qty}."""
        out: dict[str, float] = {}
        store_href = f'{self.base}/entity/store/{config.MOYSKLAD_STORE_MATERIALS_ID}'
        offset = 0
        async with aiohttp.ClientSession() as s:
            while True:
                data = await self._get(s, '/report/stock/all', {
                    'filter': f'store={store_href}', 'limit': 1000, 'offset': offset,
                })
                rows = data.get('rows', [])
                for row in rows:
                    mid = _href_id(row['meta']['href'])
                    out[mid] = row.get('stock', 0)
                if len(rows) < 1000:
                    break
                offset += 1000
        return out

    async def plan_materials_sum(self, plan_volumes: dict[str, float]) -> dict[str, dict]:
        """Суммарный расход материалов для нескольких техкарт.
        plan_volumes: {plan_id: volume}
        return: {material_id: {code, name, qty_needed}}
        """
        materials: dict[str, dict] = defaultdict(lambda: {'code': '', 'name': '', 'qty_needed': 0.0})
        async with aiohttp.ClientSession() as s:
            for plan_id, volume in plan_volumes.items():
                data = await self._get(s, f'/entity/processingplan/{plan_id}/materials', {
                    'expand': 'assortment', 'limit': 100,
                })
                for mat in data.get('rows', []):
                    a = mat.get('assortment', {}) or {}
                    mid = a.get('id', '')
                    if not mid:
                        continue
                    materials[mid]['code'] = a.get('code', '')
                    materials[mid]['name'] = a.get('name', '?')
                    materials[mid]['qty_needed'] += mat.get('quantity', 0) * volume
        return dict(materials)

    # === Поиск и редактирование существующих ПЗ ===

    async def get_task_states_map(self) -> dict[str, str]:
        """Маппинг {имя статуса в нижнем регистре: state_id} для productiontask."""
        async with aiohttp.ClientSession() as s:
            data = await self._get(s, '/entity/productiontask/metadata')
        out = {}
        for st in data.get('states', []) or []:
            name = (st.get('name') or '').strip()
            if name:
                out[name.lower()] = st['id']
        return out

    async def find_task_by_name(self, name: str) -> Optional[dict]:
        """Найти ПЗ по номеру (с zero-pad: '305' → '00305'). Возвращает dict или None."""
        # Пробуем как есть и с padding до 5 цифр
        variants = [name]
        if name.isdigit():
            variants.append(name.zfill(5))
        variants = list(dict.fromkeys(variants))  # uniq

        async with aiohttp.ClientSession() as s:
            for v in variants:
                data = await self._get(s, '/entity/productiontask', {
                    'filter': f'name={v}', 'limit': 1, 'expand': 'state',
                })
                rows = data.get('rows', [])
                if rows:
                    return rows[0]
        return None

    async def find_recent_tasks(self, limit: int = 5) -> list[dict]:
        """Последние N ПЗ (по убыванию moment)."""
        async with aiohttp.ClientSession() as s:
            data = await self._get(s, '/entity/productiontask', {
                'order': 'moment,desc', 'limit': limit, 'expand': 'state',
            })
        return data.get('rows', [])

    async def find_tasks_by_date_range(self, moment_from: str, moment_to: str, limit: int = 20) -> list[dict]:
        """ПЗ в диапазоне дат. Формат moment: 'YYYY-MM-DD HH:MM:SS'."""
        async with aiohttp.ClientSession() as s:
            data = await self._get(s, '/entity/productiontask', {
                'filter': f'moment>={moment_from};moment<={moment_to}',
                'order': 'moment,desc', 'limit': limit, 'expand': 'state',
            })
        return data.get('rows', [])

    async def get_task_by_id(self, task_id: str) -> dict:
        """Получить полное ПЗ по id вместе с раскрытым state."""
        async with aiohttp.ClientSession() as s:
            return await self._get(s, f'/entity/productiontask/{task_id}', {'expand': 'state'})

    async def get_task_rows(self, task_id: str) -> list[dict]:
        """Позиции ПЗ с раскрытым processingPlan и его products.assortment.
        Возвращает [{row_id, plan_id, plan_name, product_code, product_name, volume}]."""
        async with aiohttp.ClientSession() as s:
            data = await self._get(
                s, f'/entity/productiontask/{task_id}/productionrows',
                {'expand': 'processingPlan.products.assortment', 'limit': 100},
            )
        out = []
        for row in data.get('rows', []):
            plan = row.get('processingPlan', {}) or {}
            # Первый продукт техкарты — это что эта строка производит
            products = (plan.get('products', {}) or {}).get('rows', [])
            if products:
                a = products[0].get('assortment', {}) or {}
                product_code = a.get('code', '')
                product_name = a.get('name', '')
            else:
                product_code, product_name = '', ''
            out.append({
                'row_id': row['id'],
                'plan_id': plan.get('id', ''),
                'plan_name': plan.get('name', ''),
                'product_code': product_code,
                'product_name': product_name,
                'volume': row.get('productionVolume', 0),
            })
        return out

    async def add_task_row(self, task_id: str, plan_id: str, volume: float) -> dict:
        """POST новой позиции в ПЗ."""
        url = f'{self.base}/entity/productiontask/{task_id}/productionrows'
        payload = [{'processingPlan': _meta('processingplan', plan_id), 'productionVolume': volume}]
        async with aiohttp.ClientSession() as s:
            async with s.post(url, headers=_headers(), json=payload, ssl=_ssl_ctx()) as r:
                if r.status not in (200, 201):
                    text = await r.text()
                    raise RuntimeError(f'POST {url} → {r.status}: {text[:300]}')
                return await r.json()

    async def update_task_row_volume(self, task_id: str, row_id: str, volume: float) -> dict:
        """PUT количества в существующей позиции."""
        url = f'{self.base}/entity/productiontask/{task_id}/productionrows/{row_id}'
        payload = {'productionVolume': volume}
        async with aiohttp.ClientSession() as s:
            async with s.put(url, headers=_headers(), json=payload, ssl=_ssl_ctx()) as r:
                if r.status not in (200, 201):
                    text = await r.text()
                    raise RuntimeError(f'PUT {url} → {r.status}: {text[:300]}')
                return await r.json()

    async def delete_task_row(self, task_id: str, row_id: str) -> None:
        """DELETE позиции."""
        url = f'{self.base}/entity/productiontask/{task_id}/productionrows/{row_id}'
        async with aiohttp.ClientSession() as s:
            async with s.delete(url, headers=_headers(), ssl=_ssl_ctx()) as r:
                if r.status not in (200, 204):
                    text = await r.text()
                    raise RuntimeError(f'DELETE {url} → {r.status}: {text[:300]}')

    async def update_task_state(self, task_id: str, state_id: str) -> dict:
        """PUT на ПЗ — только смена статуса."""
        url = f'{self.base}/entity/productiontask/{task_id}'
        payload = {'state': _meta('state', state_id)}
        async with aiohttp.ClientSession() as s:
            async with s.put(url, headers=_headers(), json=payload, ssl=_ssl_ctx()) as r:
                if r.status not in (200, 201):
                    text = await r.text()
                    raise RuntimeError(f'PUT {url} → {r.status}: {text[:300]}')
                logger.info(f'Updated task {task_id} state → {state_id}')
                return await r.json()

    async def create_production_task(
        self,
        plan_volumes: dict[str, float],
        description: str,
        products_store_id: str,
        delivery_planned_moment: str,
        applicable: bool = False,
        reserve: bool = True,
    ) -> dict:
        """Создать ПЗ без привязки к заказу — всегда черновик (applicable=False).

        products_store_id: куда поступает выпуск (основа → Производство, готовка → Готовая).
        delivery_planned_moment: строка формата 'YYYY-MM-DD HH:MM:SS' — Планируемая дата выполнения.
        """
        rows = [
            {'processingPlan': _meta('processingplan', pid), 'productionVolume': qty}
            for pid, qty in plan_volumes.items()
        ]
        payload = {
            'organization': _meta('organization', config.MOYSKLAD_ORG_ID),
            'materialsStore': _meta('store', config.MOYSKLAD_STORE_MATERIALS_ID),
            'productsStore': _meta('store', products_store_id),
            'state': _meta('state', config.MOYSKLAD_PRODUCTION_TASK_STATE_PODGOTOVKA),
            'applicable': applicable,
            'reserve': reserve,
            'description': description,
            'deliveryPlannedMoment': delivery_planned_moment,
            'productionRows': rows,
        }
        async with aiohttp.ClientSession() as s:
            result = await self._post(s, '/entity/productiontask', payload)
        logger.info(f"Created production task: {result.get('name')} ({result.get('id')})")
        return result
