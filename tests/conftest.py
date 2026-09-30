from __future__ import annotations
import copy
import pytest
import yaml
from docmost_publisher.engine import DEFAULTS
from docmost_publisher.markdown_tree import SourceTree

@pytest.fixture
def cfg():
    result = copy.deepcopy(DEFAULTS)
    result['docmost'].update(url='https://wiki.example.test', space='Engineering')
    result['docmost']['root']['title'] = 'ERP documentation'
    result['docmost']['http']['request_delay_ms'] = 0
    result['publish']['verify_attempts'] = 1
    return result

@pytest.fixture
def tree_factory(tmp_path, cfg):
    counter = 0
    def build(files, changes=None):
        nonlocal counter
        counter += 1
        root = tmp_path / f'docs{counter}'
        root.mkdir()
        scratch = tmp_path / f'scratch{counter}'
        scratch.mkdir()
        current = copy.deepcopy(cfg)
        for section, values in (changes or {}).items():
            current[section].update(values)
        for name, content in files.items():
            p = root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content if isinstance(content, bytes) else content.encode())
        config_path = root / '.docmost.yaml'
        config_path.write_text(yaml.safe_dump(current))
        return SourceTree(root, config_path, current['docmost']['root']['title'], current['source'],
                          current['publish'], current['links'], current['assets'], scratch).build()
    return build
