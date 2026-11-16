MS = 'https://api.moysklad.ru/api/remap/1.2'


def product(pid, minimum):
    return {'id': pid, 'minimumBalance': minimum}


def row(pid, name, left, folder='Производство', path='', uom='шт', in_transit=0):
    return {'meta': {'href': f'{MS}/entity/product/{pid}?expand=supplier'}, 'name': name, 'stock': left,
            'inTransit': in_transit, 'uom': {'name': uom}, 'folder': {'name': folder, 'pathName': path}}


class FakeClient:
    def __init__(self, products, rows, fail=False):
        self.products, self.rows, self.fail = products, rows, fail
        self.asked, self.queries = 0, []

    async def stock_all(self, session, **modes):
        self.queries.append(modes)
        if self.fail:
            raise RuntimeError('503')
        return self.rows

    async def list_entities(self, session, entity, **kwargs):
        self.asked += 1
        return self.products


class Bot:
    def __init__(self, blocked=()):
        self.sent, self.blocked = [], set(blocked)

    async def send_message(self, chat_id, text):
        if chat_id in self.blocked:
            raise RuntimeError('Forbidden: bot was blocked by the user')
        self.sent.append((chat_id, text))


class FakeSession:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False
