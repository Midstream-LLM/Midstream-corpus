"""Replay completed calls on resume; credentials and hidden reasoning are not logged."""
from types import SimpleNamespace
from psycopg.types.json import Jsonb
from .retrieval import digest
from .models import Evidence


class Journal:
    def __init__(self, conn, item_id, before_call=None):
        self.conn, self.item_id = conn, item_id
        self.before_call = before_call

    def call(self, kind, request, invoke):
        if self.before_call:
            self.before_call()
        key = digest([kind, request])
        row = self.conn.execute('SELECT result FROM curation_call WHERE item_id=%s AND call_key=%s',
                                (self.item_id, key)).fetchone()
        self.conn.commit()
        if row:
            return row[0]
        result = invoke()
        self.conn.execute('INSERT INTO curation_call(item_id,call_key,kind,result) VALUES(%s,%s,%s,%s)',
                           (self.item_id, key, kind, Jsonb(result)))
        self.conn.commit()
        return result


class JournalServices:
    def __init__(self, services, journal):
        self.services, self.client, self.journal = services, services.client, journal

    def generate(self, prompt, **kwargs):
        _, profile, endpoint = self.client.config.resolve('curation', kwargs.get('profile'))
        request = {'prompt': prompt, **kwargs, 'model': profile.model, 'parameters': profile.parameters,
                   'endpoint': endpoint.base_url, 'protocol': endpoint.protocol, 'max_tokens': profile.max_tokens}
        def invoke():
            response = self.services.generate(prompt, **kwargs)
            return {name: getattr(response, name) for name in ('text','model','profile','response_id','usage','finish_reason')}
        return SimpleNamespace(**self.journal.call('model', request, invoke))


class JournalRetrieval:
    def __init__(self, retrieval, journal):
        self.retrieval, self.journal = retrieval, journal

    def lookup(self, action, query):
        result = self.journal.call('retrieval', [action,query],
                                   lambda: [item.model_dump() for item in self.retrieval.lookup(action,query)])
        return [Evidence.model_validate(item) for item in result]
