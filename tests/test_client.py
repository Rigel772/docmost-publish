from __future__ import annotations
from unittest.mock import Mock
import pytest
import requests
from docmost_publisher.engine import DocmostClient, APIError, EMPTY_DOC, select_root, snapshot_descendants
from docmost_publisher.markdown_tree import PublisherError


def response(status=200, body=None):
    r = requests.Response()
    r.status_code = status
    import json
    r._content = b'' if body is None else json.dumps(body).encode()
    r._content_consumed = True
    return r


def test_response_envelope(cfg):
    c = DocmostClient(cfg['docmost'])
    c.session.post = Mock(return_value=response(body={'data':{'id':'123'}}))
    assert c.post('pages/info', read_only=True) == {'id':'123'}


def test_bare_attachment_response(cfg):
    c = DocmostClient(cfg['docmost'])
    c.session.post = Mock(return_value=response(body={'id':'123','url':'/files/x'}))
    assert c.post('files/upload')['url'] == '/files/x'


def test_no_retry_after_timeout(cfg):
    c = DocmostClient(cfg['docmost'])
    c.session.post = Mock(side_effect=requests.Timeout())
    with pytest.raises(PublisherError, match='Outcome may be unknown'):
        c.create('s',None,'page')
    assert c.session.post.call_count == 1


def test_no_redirect_following(cfg):
    c = DocmostClient(cfg['docmost'])
    c.session.post = Mock(return_value=response(status=302))
    with pytest.raises(PublisherError, match='Redirect'):
        c.post('spaces')
    assert c.session.post.call_args.kwargs['allow_redirects'] is False


def test_empty_body_uses_structured_json(cfg):
    c = DocmostClient(cfg['docmost'])
    c.post = Mock()
    c.replace('p','Title','')
    payload = c.post.call_args.args[1]
    assert payload['format']=='json' and payload['content']==EMPTY_DOC and payload['operation']=='replace'


def test_paginate_even_through_filtered_empty_page(cfg):
    c = DocmostClient(cfg['docmost'])
    c.post = Mock(side_effect=[{'items':[], 'meta':{'hasNextPage':True,'nextCursor':'next'}},
                               {'items':[{'id':'1'}], 'meta':{'hasNextPage':False}}])
    assert c.all_items('spaces',{}) == [{'id':'1'}]
    assert c.post.call_args.args[1]['cursor'] == 'next'


def test_ambiguous_pagination_rejected(cfg):
    c = DocmostClient(cfg['docmost'])
    c.post = Mock(return_value={'items':[], 'meta':{'hasNextPage':True,'nextCursor':'same'}})
    with pytest.raises(PublisherError, match='pagination cursor'):
        c.all_items('spaces',{})


def test_wrong_space_child_rejected(cfg):
    c = DocmostClient(cfg['docmost'])
    c.all_items = Mock(return_value=[{'id':'p','spaceId':'other','parentPageId':'root'}])
    with pytest.raises(PublisherError, match='scope'):
        c.children('wanted','root')


def test_remote_page_limit(cfg):
    c = DocmostClient(cfg['docmost'])
    c.children = Mock(return_value=[{'id':'a','spaceId':'s','parentPageId':'root'}, {'id':'b','spaceId':'s','parentPageId':'root'}])
    with pytest.raises(PublisherError, match='max_delete_pages'):
        snapshot_descendants(c,'s','root',1)


def test_duplicate_root_title_refused(cfg):
    c = DocmostClient(cfg['docmost'])
    c.children = Mock(return_value=[{'id':'a','title':'Root'}, {'id':'b','title':'Root'}])
    with pytest.raises(PublisherError, match='Multiple top-level'):
        select_root(c,{'id':'s'}, {'id':None,'title':'Root'})
