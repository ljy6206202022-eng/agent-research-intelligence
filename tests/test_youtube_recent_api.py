"""Fixture-only official recent path, bounded pagination and failures."""
import json
import pytest
from types import SimpleNamespace
from agent_research_intelligence.connectors.youtube_sources import YouTubeSources
from agent_research_intelligence.connectors.public_http import AcquisitionError,Response
CID='UCabcdefghijklmnopqrstuv'
def item(vid='abcdefghijk'):
    return {'id':'pi-'+vid,'snippet':{'channelId':CID,'resourceId':{'videoId':vid},'title':'context engineering'},'contentDetails':{'videoId':vid,'videoPublishedAt':'2026-09-01'}}
class Account:
    def __init__(self,pages=None):self.calls=[];self.pages=list(pages or [{'items':[item()]}])
    def status(self):return {'token_present':True,'status':'READ_PERMISSION_APPROVED'}
    def read(self,endpoint,params):
        self.calls.append((endpoint,params))
        if endpoint=='channels':return {'items':[{'id':CID,'contentDetails':{'relatedPlaylists':{'uploads':'UUabcdefghijklmnopqrstuv'}}}]}
        page=self.pages.pop(0)
        if isinstance(page,Exception):raise page
        return page
class HTTP:
    def __init__(self):self.calls=0
    def conditional_get(self,*a,**kw):self.calls+=1;raise AcquisitionError('NOT_FOUND')
def test_api_normal_not_rss_and_no_rss_validator_reuse():
    a=Account();h=HTTP();response,rows=YouTubeSources(h,account=a).recent(CID,etag='rss-etag')
    assert h.calls==0 and response.headers['x-recent-provider']=='YOUTUBE_API_UPLOADS'
    assert len(rows)==1 and rows[0]['published_at']=='2026-09-01'
    assert a.calls[0]==('channels',{'part':'contentDetails','id':CID,'maxResults':1})
    assert all('etag' not in p for _,p in a.calls)
def test_rss_failure_falls_back_with_reason():
    a=Account();h=HTTP();response,rows=YouTubeSources(h,account=a,prefer_rss=True).recent(CID)
    assert h.calls==1 and rows and response.headers['x-fallback-reason']=='NOT_FOUND'
def test_no_auth_does_not_read_account():
    a=Account();a.status=lambda:{'token_present':False}
    with pytest.raises(AcquisitionError,match='NOT_FOUND'):YouTubeSources(HTTP(),account=a).recent(CID)
    assert a.calls==[]
def test_pagination_and_duplicate_video():
    a=Account([{'items':[item()], 'nextPageToken':'page2'}, {'items':[item(),item('abcdefghij2')]}])
    response,rows=YouTubeSources(HTTP(),account=a).recent(CID)
    assert len(rows)==2 and a.calls[-1][1]['pageToken']=='page2'
    assert len(json.loads(response.body)['pages'])==2
@pytest.mark.parametrize('code',['QUOTA_EXCEEDED','RATE_LIMITED','NOT_FOUND','OAUTH_TRANSPORT_OR_PARSE_FAILED'])
def test_partial_page_failure_not_success(code):
    a=Account([{'items':[item()],'nextPageToken':'p2'},AcquisitionError(code)])
    with pytest.raises(AcquisitionError,match=code):YouTubeSources(HTTP(),account=a).recent(CID)
def test_mismatched_identity_and_cursor_cycle():
    bad=item();bad['snippet']['channelId']='UCwrong'
    with pytest.raises(AcquisitionError,match='VIDEO_CHANNEL'):YouTubeSources(HTTP(),account=Account([{'items':[bad]}])).recent(CID)
    a=Account([{'items':[],'nextPageToken':'same'}]*3)
    with pytest.raises(AcquisitionError,match='PAGINATION_CYCLE'):YouTubeSources(HTTP(),account=a).recent(CID)
def test_empty_channel_is_not_no_change():
    a=Account();a.read=lambda *args:{'items':[]}
    with pytest.raises(AcquisitionError,match='CHANNEL_NOT_FOUND'):YouTubeSources(HTTP(),account=a).recent(CID)
