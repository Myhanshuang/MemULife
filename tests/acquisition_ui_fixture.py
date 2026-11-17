"""A real acquisition service + synthetic memory core for browser E2E tests."""
import json
from pathlib import Path
import tempfile

from memu.acquisition.http_api import AcquisitionHTTPServer
from memu.acquisition.service import AcquisitionService
from memu.http_transport import run_server
from test_acquisition_pipeline import CoreClient
from test_service import service_fixture


def main():
    with tempfile.TemporaryDirectory(prefix='memu-ui-fixture-') as directory:
        root = Path(directory)
        core, model, _, _ = service_fixture(root)
        client = CoreClient(core)

        class ObservedService(AcquisitionService):
            def save(self):
                super().save()
                for job in self.state['jobs'].values():
                    if job['state'] == 'paused':
                        print(json.dumps({'event': 'failed', 'error_code': job['error_code'],
                                          'write_errors': [row.get('error') for row in core.repo.state['writes'].values()]}), flush=True)
                completed = [job['job_id'] for job in self.state['jobs'].values() if job['state'] == 'completed']
                for job in completed:
                    if job not in getattr(self, 'announced', set()):
                        self.announced = getattr(self, 'announced', set()) | {job}
                        core.refresh_due(force=True)
                        print(json.dumps({'event': 'completed', 'job_id': job, 'model_calls': dict(model.calls),
                                          'materials': [item['text'] for item in client.calls]}), flush=True)

        service = ObservedService(root / 'acquisition', client, core.config)
        notes = root / 'notes'
        notes.mkdir()
        (notes / 'selected.md').write_text('UI_SELECTED_ORIGINAL')
        (notes / 'unselected.md').write_text('UI_UNSELECTED_MUST_NOT_PERSIST')
        settings = core.config.core.model_copy(update={'port': 0})
        server = AcquisitionHTTPServer(service, settings, client.tokens)
        origin = f'http://127.0.0.1:{server.server_port}'
        settings.allowed_origins = [origin]
        service.start()
        print(json.dumps({'event': 'ready', 'origin': origin, 'token': client.tokens['owner'], 'notes': str(notes)}), flush=True)
        try:
            run_server(server)
        finally:
            service.close()


if __name__ == '__main__':
    main()
