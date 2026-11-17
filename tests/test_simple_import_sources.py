"""Selected-source contracts only: no database, model or real account requests."""

import base64
from io import BytesIO
import json
from zipfile import ZipFile

import httpx
from PIL import Image
import pytest

from memu.acquisition.adapters.base import AdapterError
from memu.acquisition.adapters.immich_auto import AutoImmichAdapter
from memu.acquisition.attachments import summarize
from memu.acquisition.files import FileReader
from memu.acquisition.setup import setup_catalog
from memu.acquisition.sources import get_source
from memu.acquisition.sources.selection import Selection
from memu.memulife import snapshot_material
from test_acquisition_cloud_sources import asset, immich_schema


def test_local_files_accept_mixed_formats_and_retain_unparsed_originals(tmp_path):
    (tmp_path / 'note.txt').write_text('实际正文，不是文件名')
    (tmp_path / 'code.py').write_text('print("hello")')
    (tmp_path / 'unknown.bin').write_bytes(b'\xff\x00\x81opaque')
    (tmp_path / 'empty.log').write_bytes(b'')
    (tmp_path / 'broken.jpg').write_bytes(b'not an image')
    (tmp_path / 'page.html').write_text('<p>可读内容</p><script>noise()</script>')
    (tmp_path / 'utf16.txt').write_bytes('中文正文'.encode('utf-16'))
    with ZipFile(tmp_path / 'document.docx', 'w') as z:
        z.writestr('word/document.xml', '<w:document xmlns:w="urn:word"><w:p><w:r><w:t>文档正文</w:t>'
                   '</w:r></w:p></w:document>')
    image = BytesIO()
    Image.new('RGB', (2, 2)).save(image, format='PNG')
    (tmp_path / 'photo.png').write_bytes(image.getvalue())
    source = get_source('filesystem.typora')
    binding = {'adapter_id': source.adapter_id, 'source_profile': 'linux-markdown-v1',
               'source_namespace': 'test', 'source_instance_id': 'test', 'config': {'root': str(tmp_path)}}
    catalog = source.candidates(binding)
    assert len(catalog['items']) == 9
    frozen = source.prepare_plan(binding, Selection(scopes=['.']))
    records = FileReader(binding, frozen).read('backfill').records
    materials = {r.object_id: snapshot_material(binding, r) for r in records}
    assert materials['document.docx']['text'] == '文档正文'
    assert materials['page.html']['text'].strip() == '可读内容'
    assert materials['utf16.txt']['text'] == '中文正文'
    assert base64.b64decode(materials['photo.png']['media'][0]['data_base64']) == image.getvalue()
    for name, material in materials.items():
        audit = material['metadata']['memulife']['transport_audit']
        assert base64.b64decode(audit['raw']) == (tmp_path / name).read_bytes()
        assert material['metadata']['content']['audit_only'] == (name in {'unknown.bin', 'empty.log', 'broken.jpg'})
    assert summarize(list(materials.values())) == {'available': 9, 'pending': 0, 'excluded': 0, 'unparsed': 3}
    (tmp_path / 'note.txt').write_text('changed')
    with pytest.raises(AdapterError, match='import_candidate_changed'):
        FileReader(binding, frozen).read('backfill')


def connection(monkeypatch, *, foreign=False, thumbnail_status=200):
    monkeypatch.setenv('IMMICH_TEST_KEY', 'private-test-key')
    schema = immich_schema()
    schema['components']['schemas']['SearchFilter']['properties'].update(takenAt={}, originalFileName={})
    calls = []

    def handle(request):
        assert request.headers['x-api-key'] == 'private-test-key'
        path = request.url.path
        calls.append((path, json.loads(request.content) if request.content else None))
        if path == '/api/spec.json':
            return httpx.Response(200, json=schema)
        if path == '/api/users/me':
            return httpx.Response(200, json={'id': 'owner-1'})
        if path == '/api/server/version':
            return httpx.Response(200, json={'major': 3, 'minor': 2, 'patch': 0})
        if path == '/api/albums':
            return httpx.Response(200, json=[{'id': 'album-1', 'albumName': '旅行', 'assetCount': 1}])
        if path == '/api/search/metadata':
            body = json.loads(request.content)
            filters = body['filter']
            identity = filters.get('id', {}).get('eq', 'asset-1')
            rows = [] if filters['visibility']['eq'] == 'archive' else [
                asset(identity, ownerId='other' if foreign else 'owner-1')]
            return httpx.Response(200, json={'assets': {'items': rows, 'nextCursor': None}})
        if path.endswith('/original'):
            return httpx.Response(200, content=b'original', headers={'content-type': 'image/jpeg'})
        if path.endswith('/thumbnail'):
            assert request.url.params['size'] == 'thumbnail'
            image = BytesIO()
            Image.new('RGB', (8, 8)).save(image, format='JPEG')
            return httpx.Response(thumbnail_status, content=image.getvalue(), headers={'content-type': 'image/jpeg'})
        if path.startswith('/api/assets/'):
            return httpx.Response(200, json=asset(path.rsplit('/', 1)[-1]))
        raise AssertionError(path)

    return {'base_url': 'https://photos.example/', 'api_key_ref': 'env:IMMICH_TEST_KEY'}, calls, httpx.Client(
        transport=httpx.MockTransport(handle))


def test_immich_two_field_setup_and_metadata_only_browse(monkeypatch):
    setup = next(a for a in setup_catalog()['adapters'] if a['adapter_id'] == 'immich.assets')
    assert len(setup['profiles']) == 1
    assert set(setup['profiles'][0]['schema']['properties']) == {'base_url', 'api_key_ref'}
    config, calls, client = connection(monkeypatch)
    with client:
        reader = AutoImmichAdapter(config, client)
        assert [r['id'] for r in reader.catalog()['items']] == ['album_ids:album-1', 'all_assets']
        first = reader.catalog('album_ids:album-1', query='照片')
        assert first['items'][0]['id'] == 'asset_ids:asset-1'
        body = calls[-1][1]
        assert body['filter']['albumIds'] == {'any': ['album-1']}
        assert body['filter']['originalFileName'] == {'like': '%照片%'}
        assert reader.catalog('album_ids:album-1', first['next_cursor'])['next_cursor'] is None
        assert not any('/assets/' in path for path, _ in calls)


def test_immich_selected_asset_and_date_scope_before_reference(monkeypatch):
    config, calls, client = connection(monkeypatch)
    selection = Selection(scopes=['asset_ids:chosen'], start='2019-01-01T00:00:00Z', end='2020-01-01T00:00:00Z')
    narrowed = get_source('immich.assets').narrow_config(config, selection)
    with client:
        page = AutoImmichAdapter(narrowed, client).read('backfill', limit=1)
    assert not page.has_more and [r.object_id for r in page.records] == ['asset:chosen']
    assert page.records[0].occurred_at.startswith('2019')
    searches = [body for path, body in calls if path.endswith('/search/metadata')]
    assert all(body['filter']['id'] == {'eq': 'chosen'} and 'takenAt' in body['filter'] for body in searches)
    assert not any(path.endswith('/original') for path, _ in calls)
    assert page.records[0].attachments[0].status == 'referenced'
    from memu.memulife import snapshot_material
    from memu.acquisition.attachments import summarize
    material = snapshot_material({'adapter_id': 'immich.assets', 'source_profile': 'api-key',
        'source_instance_id': 'fixture', 'source_namespace': '相册'}, page.records[0])
    assert material['media'] == []
    assert '未读取图片或视频原件' in material['text']
    assert summarize([material])['referenced'] == 1
    assert summarize([material])['pending'] == 0
    config, calls, client = connection(monkeypatch, foreign=True)
    with client, pytest.raises(AdapterError, match='account_scope_mismatch'):
        AutoImmichAdapter({**config, 'asset_ids': ['chosen']}, client).read('backfill')
    assert not any(path.endswith('/original') for path, _ in calls)


@pytest.mark.parametrize('thumbnail_status', [200, 403])
def test_immich_memory_preview_is_transient_and_resolves_bound_asset(monkeypatch, thumbnail_status):
    from types import SimpleNamespace
    import hashlib
    from memu.acquisition.memory_media import memory_media
    config, calls, client = connection(monkeypatch, thumbnail_status=thumbnail_status)
    item = {'ref': 'a1', 'kind': 'immich', 'asset_id': 'asset-1', 'source_id': 'connection-one',
            'owner_id': 'owner-1', 'deployment_id': hashlib.sha256(b'https://photos.example/api').hexdigest()}
    binding = {'adapter_id': 'immich.assets', 'source_profile': 'api-key', 'config': config}
    service = SimpleNamespace(owner=lambda identity: None, binding=lambda identity, source_id: binding,
        client=SimpleNamespace(browse=lambda *args, **kw: {'media': [item]}))
    source = get_source('immich.assets')
    monkeypatch.setattr(type(source), 'reader_class', lambda self, profile: lambda cfg: AutoImmichAdapter(cfg, client))
    result = memory_media(service, 'owner', 'a1')
    assert result['open_url'] == 'https://photos.example/photos/asset-1'
    assert not result['content_persisted']
    assert bool(result['thumbnail']) == (thumbnail_status == 200)
    assert not any(path.endswith('/original') for path, _ in calls)
    assert 'fixture-api-key' not in json.dumps(result)
    item['owner_id'] = 'another-owner'
    before = len([path for path, _ in calls if path.endswith('/thumbnail')])
    with pytest.raises(AdapterError, match='source_binding_mismatch'):
        memory_media(service, 'owner', 'a1')
    assert len([path for path, _ in calls if path.endswith('/thumbnail')]) == before
    client.close()


def test_immich_mixed_album_and_asset_selection_is_union(monkeypatch):
    config, calls, client = connection(monkeypatch)
    config = get_source('immich.assets').narrow_config(config, Selection(scopes=['album_ids:album-1', 'asset_ids:outside']))
    records, cursor = [], None
    with client:
        reader = AutoImmichAdapter(config, client)
        for _ in range(10):
            page = reader.read('backfill', cursor, limit=1)
            records.extend(page.records)
            cursor = page.cursor
            if not page.has_more:
                break
    assert not page.has_more
    assert {r.object_id for r in records} == {'asset:asset-1', 'asset:outside'}
    outside = [body for path, body in calls if body and body.get('filter', {}).get('id', {}).get('eq') == 'outside']
    assert outside and all('albumIds' not in body['filter'] for body in outside)


@pytest.mark.parametrize('thumbnail_status', [200, 403])
def test_immich_album_preview_is_transient_and_date_scoped(monkeypatch, thumbnail_status):
    from memu.acquisition.sources.immich_preview import preview

    config, calls, client = connection(monkeypatch, thumbnail_status=thumbnail_status)
    selection = Selection(scopes=['album_ids:album-1'], start='2019-01-01T00:00:00Z', end='2020-01-01T00:00:00Z')
    narrowed = get_source('immich.assets').narrow_config(config, selection)
    with client:
        reader = AutoImmichAdapter(narrowed, client)
        result = preview(reader, selection.scopes[0], '', 24)
        assert not result['content_persisted']
        assert result['items'][0]['id'] == 'asset_ids:asset-1'
        if thumbnail_status == 200:
            assert result['items'][0]['thumbnail'].startswith('data:image/jpeg;base64,')
        else:
            assert result['warnings'] == ['immich_preview_permission_required']
        assert all('/original' not in path for path, _ in calls)
        body = next(body for path, body in calls if path.endswith('/search/metadata'))
        assert body['filter']['albumIds'] == {'any': ['album-1']} and 'takenAt' in body['filter']
        with pytest.raises(AdapterError, match='import_preview_cursor_invalid'):
            preview(reader, 'all_assets', result['next_cursor'], 24)


def test_immich_preview_rejects_foreign_assets_before_thumbnail(monkeypatch):
    from memu.acquisition.sources.immich_preview import preview

    config, calls, client = connection(monkeypatch, foreign=True)
    with client, pytest.raises(AdapterError, match='account_scope_mismatch'):
        preview(AutoImmichAdapter(config, client), 'all_assets', '', 24)
    assert not any('/assets/' in path for path, _ in calls)
