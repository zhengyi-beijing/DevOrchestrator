"""The generated fixture must enter the existing native lifecycle without adapters."""
from pathlib import Path
import tempfile
from dev_orchestrator.soak.permit import create_soak_permit
from dev_orchestrator.soak.template import generate_soak_project
from dev_orchestrator.monitor.telemetry import extract_task_id
from dev_orchestrator.config import load_projects_config


def test_generated_tasks_use_native_identity_and_real_ai_configuration():
    with tempfile.TemporaryDirectory(prefix='p20-template-native-') as temporary:
        root = Path(temporary)
        canonical = root / 'canonical'
        canonical.mkdir()
        permit = create_soak_permit('native-contract', 'native-project', root / 'repo', root / 'runtime',
                                    canonical, canonical / 'runtime')
        manifest = generate_soak_project(permit)
        config = load_projects_config(manifest['config_path'])
        assert config['projects'][0]['execution']['engine'] == 'aibroker'
        assert config['projects'][0]['ai_roles']['planner']['enabled'] is True
        for task in manifest['task_ids']:
            title = (root / 'repo/agent/staged' / (task + '.md')).read_text(encoding='utf-8').splitlines()[0]
            assert extract_task_id(title) == task
        assert 'NotImplementedError' in (root / 'repo/soak_app.py').read_text(encoding='utf-8')
        assert 'class RecordStore' not in (root / 'repo/soak_app.py').read_text(encoding='utf-8')
        assert generate_soak_project(permit)['initial_commit'] == manifest['initial_commit']
