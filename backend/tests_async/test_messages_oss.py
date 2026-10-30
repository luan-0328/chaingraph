"""领取竞争、独立未读游标、私有图片与失败补偿；不访问云账户。"""
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from types import SimpleNamespace
from PIL import Image
import pytest
from app.core.exceptions import BusinessError
from app.services import attachment_service as attachments
from app.services import oss_storage
from tests_async.conftest import execute, query


def png():
    result = BytesIO()
    Image.new('RGB', (12, 12), 'blue').save(result, format='PNG')
    return result.getvalue()


def viewer(client, role='USER'):
    return {'id': client.user_id, 'role': role}


class Storage:
    bucket = 'isolated-test'
    def __init__(self):
        self.objects = {}
        self.fail_upload = self.fail_delete = False
    def upload(self, bucket, key, data, content_type):
        self.objects[key] = data
        if self.fail_upload:
            raise OSError('simulated timeout after write')
    def delete(self, bucket, key):
        if self.fail_delete:
            raise OSError('simulated deletion failure')
        self.objects.pop(key, None)
    def signed_url(self, bucket, key, seconds):
        assert key in self.objects and seconds == 300
        return 'https://example.invalid/private-test-image'


def test_atomic_claim_has_one_winner(app):
    case = app.scenario()
    app.user.ok('POST', f"tickets/{case['ticket']}/request-human", json={})
    execute(app.engine, "UPDATE users SET role='CUSTOMER_SERVICE' WHERE id=:id", id=app.other.user_id)
    clients = [app.staff, app.other]
    path = f"customer-service/tickets/{case['ticket']}/assign"
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda client: client.status('POST', path, json={}), clients))
    assert sorted(results) == [200, 409]
    winner, loser = clients[results.index(200)], clients[results.index(409)]
    winner.ok('POST', path, json={})
    assert loser.status('POST', path, json={'assignee_id': None}) == 409
    assert app.detail(case)['assignee_id'] == winner.user_id


def test_unread_cursor_never_regresses(app):
    case = app.scenario()
    path = f"tickets/{case['ticket']}"
    initial = app.detail(case)
    assert initial['unread_count'] == 0
    last = max(m['message_id'] for m in initial['messages'])
    app.staff.ok('POST', path + '/read', json={'last_message_id': last})
    sent = app.user.ok('POST', path + '/messages', json={'content': '补充情况'})
    latest = sent['message_id']
    staff_path = f"customer-service/{path}"
    assert app.staff.ok('GET', staff_path)['unread_count'] == 1
    assert app.staff.ok('GET', staff_path)['unread_count'] == 1
    app.staff.ok('POST', path + '/read', json={'last_message_id': latest})
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda cursor: app.staff.ok('POST', path + '/read', json={'last_message_id': cursor}), [0,last,latest,last]))
    assert app.staff.ok('GET', staff_path)['unread_count'] == 0
    assert app.admin.ok('GET', staff_path)['unread_count'] >= 2
    assert app.other.status('POST', path + '/read', json={'last_message_id': latest}) == 403
    foreign = app.detail(app.scenario())['messages'][0]['message_id']
    assert app.user.status('POST', path + '/read', json={'last_message_id': foreign}) in (400,422)
    assert app.user.ok('GET', 'tickets/unread-summary')['unread_messages'] == 0


def test_private_images_permissions_and_metadata(app, monkeypatch):
    storage = Storage()
    monkeypatch.setattr(attachments, 'get_storage', lambda: storage)
    case = app.scenario()
    kwargs = dict(ticket_id=case['ticket'], viewer=viewer(app.user), filename='../../证据.png', declared_type='image/png', data=png())
    message = attachments.upload_image(**kwargs)
    info = message['attachments'][0]
    assert info['filename'] == '证据.png'
    assert 'object_key' not in info and len(storage.objects) == 1
    assert app.detail(case)['messages'][-1]['attachments'] == [info]
    assert attachments.image_url(ticket_id=case['ticket'], attachment_id=info['attachment_id'], viewer=viewer(app.user))['expires_in'] == 300
    with pytest.raises(BusinessError) as denied:
        attachments.image_url(ticket_id=case['ticket'], attachment_id=info['attachment_id'], viewer=viewer(app.other))
    assert denied.value.http_status == 403
    with pytest.raises(BusinessError):
        attachments.upload_image(**{**kwargs, 'viewer': viewer(app.staff, 'CUSTOMER_SERVICE')})
    app.user.ok('POST', f"tickets/{case['ticket']}/request-human", json={})
    app.staff.ok('POST', f"customer-service/tickets/{case['ticket']}/assign", json={})
    attachments.upload_image(**{**kwargs, 'viewer': viewer(app.staff, 'CUSTOMER_SERVICE')})
    assert app.detail(case)['unread_count'] == 1
    assert attachments.cleanup_uploads() == 0 and len(storage.objects) == 2


def test_failed_upload_cleanup_retries(app, monkeypatch):
    storage = Storage()
    storage.fail_upload = storage.fail_delete = True
    monkeypatch.setattr(attachments, 'get_storage', lambda: storage)
    case = app.scenario()
    with pytest.raises(BusinessError) as failure:
        attachments.upload_image(ticket_id=case['ticket'], viewer=viewer(app.user), filename='x.png', declared_type='image/png', data=png())
    assert failure.value.http_status == 502
    assert query(app.engine, "SELECT status FROM ticket_attachments WHERE ticket_id=:id", id=case['ticket']) == [{'status':'CLEANUP_PENDING'}]
    assert not app.detail(case)['messages'][-1]['attachments']
    storage.fail_delete = False
    assert attachments.cleanup_uploads() == 1
    assert storage.objects == {}


@pytest.mark.parametrize('data,mime', [(b'fake image','image/png'), (png(),'image/jpeg'), (b'','image/png'), (b'x'*(5*1024*1024+1),'image/png')])
def test_images_are_validated(data, mime):
    with pytest.raises(BusinessError):
        attachments.validate_image(data, mime)


def test_blank_credentials_upload_returns_503(app):
    case = app.scenario()
    response = app.user.request('POST', f"tickets/{case['ticket']}/attachments", files={'file':('a.png',png(),'image/png')})
    assert response.status_code == 503
    assert response.json()['code'] == 'OSS_NOT_CONFIGURED'


def test_sdk_requests_are_private_and_presigned(monkeypatch):
    import alibabacloud_oss_v2 as sdk
    calls = []
    class Client:
        def __init__(self, config):
            assert config.region == 'cn-hangzhou'
        def put_object(self, request):
            calls.append(request)
        def delete_object(self, request):
            calls.append(request)
        def presign(self, request, expires):
            assert expires.total_seconds() == 300
            return SimpleNamespace(url='https://example.invalid/signed')
    monkeypatch.setattr(sdk, 'Client', Client)
    for name, value in [('oss_access_key_id','test'),('oss_access_key_secret','test'),('oss_region','cn-hangzhou'),('oss_bucket','private-test')]:
        monkeypatch.setattr(oss_storage.settings, name, value)
    storage = oss_storage.OssStorage()
    storage.upload(storage.bucket,'random.png',png(),'image/png')
    assert calls[0].acl == 'private' and calls[0].forbid_overwrite is True
    assert storage.signed_url(storage.bucket,'random.png',300).endswith('/signed')
    storage.delete(storage.bucket,'random.png')
    assert calls[-1].key == 'random.png'


def test_upload_http_success_and_signed_url(app, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app as api
    storage = Storage()
    monkeypatch.setattr(attachments, 'get_storage', lambda: storage)
    case = app.scenario()
    with TestClient(api) as client:
        headers = {'Authorization': app.user.raw.headers['Authorization']}
        path = f"/api/tickets/{case['ticket']}/attachments"
        response = client.post(path, headers=headers, files={'file':('proof.png',png(),'image/png')})
        assert response.status_code == 201, response.text
        info = response.json()['attachments'][0]
        image_path = path + f"/{info['attachment_id']}/url"
        assert client.get(image_path, headers=headers).json()['expires_in'] == 300
        assert client.get(image_path, headers={'Authorization':app.other.raw.headers['Authorization']}).status_code == 403
        assert client.post(path, headers=headers, files={'file':('fake.png',b'fake','image/png')}).status_code in (400,422)


def test_assignment_changes_during_upload_are_rechecked(app, monkeypatch):
    storage = Storage()
    case = app.scenario()
    ticket = case['ticket']
    app.user.ok('POST', f'tickets/{ticket}/request-human', json={})
    path = f'customer-service/tickets/{ticket}/assign'
    app.staff.ok('POST', path, json={})
    upload = storage.upload
    def transfer_during_upload(*args):
        upload(*args)
        app.admin.ok('POST', path, json={'assignee_id':app.admin.user_id})
    storage.upload = transfer_during_upload
    monkeypatch.setattr(attachments, 'get_storage', lambda: storage)
    with pytest.raises(BusinessError) as denied:
        attachments.upload_image(ticket_id=ticket, viewer=viewer(app.staff,'CUSTOMER_SERVICE'),filename='x.png',declared_type='image/png',data=png())
    assert denied.value.http_status == 403
    assert storage.objects == {}
    assert query(app.engine, 'SELECT id FROM ticket_attachments WHERE ticket_id=:id',id=ticket) == []


def test_ambiguous_commit_keeps_successful_image(app, monkeypatch):
    from contextlib import contextmanager
    storage = Storage()
    monkeypatch.setattr(attachments, 'get_storage', lambda: storage)
    original = attachments.session_scope
    calls = 0
    @contextmanager
    def lost_commit_ack():
        nonlocal calls
        calls += 1
        current = calls
        with original() as db:
            yield db
        if current == 3:
            raise OSError('connection lost after successful commit')
    monkeypatch.setattr(attachments, 'session_scope', lost_commit_ack)
    case = app.scenario()
    message = attachments.upload_image(ticket_id=case['ticket'],viewer=viewer(app.user),filename='x.png',declared_type='image/png',data=png())
    assert message['attachments'] and len(storage.objects) == 1
    rows = query(app.engine, 'SELECT status FROM ticket_attachments WHERE ticket_id=:id',id=case['ticket'])
    assert rows == [{'status':'READY'}]


def test_slow_oss_cleanup_does_not_block_agent_queue(app, processes):
    import os
    from pathlib import Path
    from tests_async.conftest import eventually
    folder = Path(os.environ['ASYNC_TEST_FOLDER'])
    started, release = folder / 'oss-cleanup-started', folder / 'oss-cleanup-release'
    started.unlink(missing_ok=True)
    release.unlink(missing_ok=True)
    processes.worker(fault='slow_upload_cleanup', concurrency=1)
    try:
        eventually(started.exists, description='OSS 清理开始')
        case = app.scenario()
        app.submit(case)
        eventually(lambda: app.run(case)['status'] == 'COMPLETED', timeout=5, description='清理等待期间继续处理工单')
        assert app.refund_count(case) == 1
    finally:
        release.touch()
