from __future__ import annotations
import pytest
from docmost_publisher.markdown_tree import PublisherError, load_yaml


def urls(tree):
    return {key: 'https://remote.test/' + str(i) for i, key in enumerate(tree.nodes)}


def test_nested_hierarchy_and_indexes(tree_factory):
    tree = tree_factory({'README.md':'# Ignored root title\nHello', 'architecture/README.md':'# Architecture',
                         'architecture/storage/s3.md':'# S3', 'api/auth.md':'# Authentication', 'images/a.png':b'png'})
    assert list(n.key for n in tree.ordered()) == ['.', 'api', 'api/auth.md', 'architecture', 'architecture/storage', 'architecture/storage/s3.md']
    assert tree.nodes['.'].title == 'ERP documentation'
    assert tree.aliases['architecture/README.md'] == 'architecture'
    assert 'images' not in tree.nodes
    assert tree.nodes['api/auth.md'].parent == 'api'


def test_relative_reference_directory_and_root_links(tree_factory):
    tree = tree_factory({'README.md':'[A][ref]\n\n[ref]: api/a.md "Title"\n',
                         'api/a.md':'# A\n[Home](../README.md) [Directory](../api/) [Root](/README.md)'})
    result = tree.render(tree.nodes['.'], urls(tree), lambda *_: '')
    assert urls(tree)['api/a.md'] in result
    assert 'api/a.md' not in result
    result = tree.render(tree.nodes['api/a.md'], urls(tree), lambda *_: '')
    assert urls(tree)['.'] in result and urls(tree)['api'] in result


def test_code_not_rewritten(tree_factory):
    tree = tree_factory({'README.md':'# Code\n\n```md\n[not a link](missing.md)\n```\n\n`[not a link](missing.md)`\n'})
    result = tree.render(tree.nodes['.'], urls(tree), lambda *_: '')
    assert result.count('[not a link](missing.md)') == 2


def test_asset_snapshotted_and_referenced(tree_factory):
    tree = tree_factory({'README.md':'# Image\n![Alt](images/x.png)\n', 'images/x.png':b'original'})
    tree.assets['images/x.png'].path.write_bytes(b'changed')
    calls = []
    def upload(key, asset):
        calls.append((key, asset.snapshot.read_bytes()))
        return 'https://remote.test/files/1/x.png'
    result = tree.render(tree.nodes['.'], urls(tree), upload)
    assert calls == [('.', b'original')]
    assert '![Alt]' in result and '/files/1/x.png' in result


def test_titles_are_exact_file_and_directory_names(tree_factory):
    tree = tree_factory({'README.md':'# Root heading', '0006-Tasks-Module.md':'---\ntitle: Explicit\n---\n# Heading',
                         'ADR/README.md':'# ADR heading', 'ADR/x.md':'body', 'Misc Notes/y.md':'# Y'})
    titles = {k: n.title for k, n in tree.nodes.items()}
    assert titles == {'.':'ERP documentation', '0006-Tasks-Module.md':'0006-Tasks-Module', 'ADR':'ADR',
                      'ADR/x.md':'x', 'Misc Notes':'Misc Notes', 'Misc Notes/y.md':'y'}


def test_frontmatter_not_published(tree_factory):
    tree = tree_factory({'README.md':'root', 'a.md':'---\ntitle: Explicit\nprivate: not-published\n---\n# Heading\nBody'})
    result = tree.render(tree.nodes['a.md'], urls(tree), lambda *_: '')
    assert 'not-published' not in result


def test_generated_index(tree_factory):
    tree = tree_factory({'a/b.md':'# Child'})
    result = tree.render(tree.nodes['a'], urls(tree), lambda *_: '')
    assert '## Contents' in result and '[b]' in result


@pytest.mark.parametrize('body', ['[broken](missing.md)', '![missing](missing.png)', '[secret](../secret.txt)',
                                  '[secret](.env)', '<img src="local.png">', '[local](file:///tmp/a.md)'])
def test_invalid_links_warn_and_are_kept(tree_factory, body):
    tree = tree_factory({'README.md':body})
    assert tree.warnings
    assert not tree.assets
    result = tree.render(tree.nodes['.'], urls(tree), lambda *_: pytest.fail('nothing to upload'))
    assert result.strip()


def test_link_outside_source_is_not_read(tree_factory, tmp_path):
    (tmp_path / 'outside.png').write_bytes(b'secret')
    tree = tree_factory({'README.md':'![x](../outside.png) [code](../app/x.py)'})
    assert not tree.assets
    assert 'escapes source directory' in ' '.join(tree.warnings)


def test_empty_tree_refused(tree_factory):
    with pytest.raises(PublisherError, match='No Markdown'):
        tree_factory({'notes.txt':'not markdown'})


def test_intentionally_empty_markdown_allowed(tree_factory):
    tree = tree_factory({'README.md':''})
    assert not tree.render(tree.nodes['.'], urls(tree), lambda *_: '').strip()


def test_hidden_and_excluded_files(tree_factory):
    tree = tree_factory({'README.md':'root', '.hidden/secret.md':'ignore', 'node_modules/foo/a.md':'ignore', 'drafts/a.md':'ignore'},
                        {'source':{'exclude':['**/node_modules/**', 'drafts/**']}})
    assert set(tree.nodes) == {'.'}


def test_duplicate_index_rejected(tree_factory):
    with pytest.raises(PublisherError, match='Multiple directory index'):
        tree_factory({'README.md':'a', 'readme.md':'b'})


def test_duplicate_yaml_keys_rejected():
    with pytest.raises(PublisherError, match='Duplicate YAML'):
        load_yaml('a: 1\na: 2\n')


def test_page_limit(tree_factory):
    with pytest.raises(PublisherError, match='Page count'):
        tree_factory({'README.md':'r', 'a.md':'a'}, {'publish':{'max_pages':1}})


def test_size_limit(tree_factory):
    with pytest.raises(PublisherError, match='size limit'):
        tree_factory({'README.md':'too large'}, {'source':{'max_markdown_bytes':3}})


def test_fragments(tree_factory):
    tree = tree_factory({'README.md':'[A](a.md#section)', 'a.md':'# A'})
    assert '#section' in tree.render(tree.nodes['.'], urls(tree), lambda *_: '')
    assert tree.warnings


def test_drop_fragments(tree_factory):
    tree = tree_factory({'README.md':'[A](a.md#section)', 'a.md':'# A'}, {'links':{'fragment_policy':'drop'}})
    assert '#section' not in tree.render(tree.nodes['.'], urls(tree), lambda *_: '')


def test_markdown_extensions_render(tree_factory):
    tree = tree_factory({'README.md':'''# Extensions

- [ ] Pending
- [x] Done

~~old~~ **bold** *italic*

| A | B |
|:--|--:|
| x | `y` |

Inline $x+y$.

$$
x^2
$$

Note[^n].

[^n]: Text.
'''})
    result = tree.render(tree.nodes['.'], urls(tree), lambda *_: '')
    for expected in ['[ ] Pending', '[x] Done', '~~old~~', '| A | B |', '$x+y$', '$$', '[^1]']:
        assert expected in result


def test_percent_encoded_unicode_link(tree_factory):
    tree = tree_factory({'README.md':'[A](za%C5%BC%C3%B3%C5%82%C4%87.md)', 'zażółć.md':'# Unicode'})
    result = tree.render(tree.nodes['.'], urls(tree), lambda *_: '')
    assert urls(tree)['zażółć.md'] in result
