"""Anonymous known-channel and open-world discovery; never a subscription claim."""
import hashlib
import json
import re
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

from agent_research_intelligence.acquisition.youtube import video_id, player_response
from agent_research_intelligence.connectors.public_http import AcquisitionError, Response
from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.governance.policy import require_public


def channel_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'UC[A-Za-z0-9_-]{22}',value):
        raise BoundaryError('Channel ID required; display names are not identity')
    return value


def text(value):
    if isinstance(value,str):return value
    return value.get('simpleText') or ''.join(x.get('text','') for x in value.get('runs',[]))


def walk(value):
    if isinstance(value,dict):
        yield value
        for child in value.values():yield from walk(child)
    elif isinstance(value,list):
        for child in value:yield from walk(child)


class YouTubeSources:
    def __init__(self,http,guard=lambda:None,*,account=None,prefer_rss=False):
        self.http=http;self.guard=guard;self.account=account;self.prefer_rss=prefer_rss

    def recent(self,channel,*,etag=None,modified=None):
        """Authenticated uploads is the normal path; RSS validators never enter API reads."""
        cid=channel_id(channel)
        ready=self.account is not None and self.account.status().get('token_present') and self.account.status().get('status')=='READ_PERMISSION_APPROVED'
        if ready and not self.prefer_rss:return self._uploads(cid)
        try:
            response,entries=self._rss(cid,etag=etag,modified=modified)
            return Response(response.url,response.status,response.body,{**response.headers,'x-recent-provider':'RSS'}),entries
        except AcquisitionError as exc:
            if not ready:raise
            return self._uploads(cid,fallback_reason=exc.code)

    def _uploads(self,cid,*,fallback_reason=None,max_pages=3):
        self.guard()
        channel=self.account.read('channels',{'part':'contentDetails','id':cid,'maxResults':1})
        items=channel.get('items',[])
        if not items:raise AcquisitionError('CHANNEL_NOT_FOUND')
        if len(items)!=1 or items[0].get('id')!=cid:raise AcquisitionError('CHANNEL_API_IDENTITY_MISMATCH')
        uploads=items[0].get('contentDetails',{}).get('relatedPlaylists',{}).get('uploads')
        if not isinstance(uploads,str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,100}',uploads):raise AcquisitionError('UPLOADS_PLAYLIST_MISSING')
        entries=[];seen=set();tokens=set();cursor=None;pages=[]
        for n in range(max_pages):
            params={'part':'snippet,contentDetails','playlistId':uploads,'maxResults':30-len(entries)}
            if cursor:params['pageToken']=cursor
            self.guard();page=self.account.read('playlistItems',params);pages.append(page)
            for item in page.get('items',[]):
                sn=item.get('snippet',{});detail=item.get('contentDetails',{})
                if sn.get('channelId')!=cid or sn.get('videoOwnerChannelId',cid)!=cid:raise AcquisitionError('VIDEO_CHANNEL_MISMATCH')
                vid=detail.get('videoId');video_id('https://www.youtube.com/watch?v='+str(vid))
                if sn.get('resourceId',{}).get('videoId')!=vid:raise AcquisitionError('PLAYLIST_VIDEO_IDENTITY_MISMATCH')
                if vid in seen:continue
                seen.add(vid)
                entries.append({'video_id':vid,'channel_id':cid,'title':sn.get('title',''),
                    'published_at':detail.get('videoPublishedAt'),'playlist_added_at':sn.get('publishedAt'),
                    'updated_at':None,'url':'https://www.youtube.com/watch?v='+vid,
                    'locator':'https://www.googleapis.com/youtube/v3/playlistItems#'+str(item.get('id')),
                    'provider':'YOUTUBE_API_UPLOADS','uploads_playlist':uploads,'page_index':n})
                if len(entries)>=30:break
            cursor=page.get('nextPageToken')
            if len(entries)>=30:break
            if not cursor:break
            if cursor in tokens:raise AcquisitionError('PLAYLIST_PAGINATION_CYCLE')
            tokens.add(cursor)
        body=json.dumps({'provider':'YOUTUBE_API_UPLOADS','channel':channel,'pages':pages,
            'recent_window_limit':30,'older_pages_available':bool(cursor)},sort_keys=True,separators=(',',':')).encode()
        return Response('https://www.googleapis.com/youtube/v3/playlistItems?playlistId='+uploads,200,body,
            {'x-recent-provider':'YOUTUBE_API_UPLOADS','x-fallback-reason':fallback_reason or ''}),entries

    def _rss(self,channel,*,etag=None,modified=None):
        cid=channel_id(channel);self.guard()
        url='https://www.youtube.com/feeds/videos.xml?'+urlencode({'channel_id':cid})
        response=self.http.conditional_get(url,etag=etag,modified=modified)
        if response.status==304:return response,[]
        if re.search(br'<!\s*(?:DOCTYPE|ENTITY)',response.body,re.I):raise AcquisitionError('UNSAFE_FEED_XML')
        try:
            root=ET.fromstring(response.body)
            ns={'a':'http://www.w3.org/2005/Atom','yt':'http://www.youtube.com/xml/schemas/2015'}
            if root.findtext('yt:channelId',namespaces=ns)!=cid:raise AcquisitionError('CHANNEL_FEED_IDENTITY_MISMATCH')
            entries=[]
            for n,e in enumerate(root.findall('a:entry',ns)[:30]):
                identifier=e.findtext('yt:videoId',namespaces=ns)
                video_id('https://www.youtube.com/watch?v='+str(identifier))
                if e.findtext('yt:channelId',namespaces=ns)!=cid:raise AcquisitionError('VIDEO_CHANNEL_MISMATCH')
                entries.append({'video_id':identifier,'channel_id':cid,'title':e.findtext('a:title',default='',namespaces=ns),
                    'published_at':e.findtext('a:published',namespaces=ns),'updated_at':e.findtext('a:updated',namespaces=ns),
                    'url':'https://www.youtube.com/watch?v='+identifier,'locator':url+'#entry/'+str(n)})
            return response,entries
        except ET.ParseError:raise AcquisitionError('YOUTUBE_FEED_PARSE_FAILED') from None

    def metadata(self,url):
        identifier=video_id(url);self.guard()
        response=self.http.get('https://www.youtube.com/watch?v='+identifier)
        data=player_response(response.body.decode('utf-8',errors='replace'));d=data.get('videoDetails',{})
        if d.get('videoId')!=identifier or 'shortDescription' not in d:raise AcquisitionError('INCOMPLETE_VIDEO_METADATA')
        if data.get('playabilityStatus',{}).get('status')!='OK':raise AcquisitionError('VIDEO_UNAVAILABLE_OR_AUTH_REQUIRED')
        meta={'video_id':identifier,'title':d.get('title',''),'description':d['shortDescription'],
              'channel_id':channel_id(d['channelId']),'channel':d.get('author'),
              'published_at':data.get('microformat',{}).get('playerMicroformatRenderer',{}).get('publishDate'),
              'duration_s':int(d.get('lengthSeconds',0)),'thumbnail':d.get('thumbnail',{}).get('thumbnails',[]),
              'tags':d.get('keywords'),'chapters':description_chapters(d['shortDescription']),'metadata_sha256':hashlib.sha256(response.body).hexdigest()}
        return response,meta

    def search(self,query,*,count=20):
        require_public(query)
        if not 1<=len(query.text)<=300 or not 1<=count<=40:raise BoundaryError('Bounded public search required')
        self.guard();response=self.http.get('https://www.youtube.com/results?'+urlencode({'search_query':query.text}))
        page=response.body.decode('utf-8',errors='replace');data=None
        for marker in ('var ytInitialData =','ytInitialData =','ytInitialData='):
            pos=page.find(marker)
            if pos>=0:
                try:data,_=json.JSONDecoder().raw_decode(page[pos+len(marker):].lstrip());break
                except ValueError:continue
        if data is None:raise AcquisitionError('YOUTUBE_SEARCH_STRUCTURE_UNAVAILABLE')
        out=[];seen=set()
        for node in walk(data):
            if 'videoRenderer' in node:
                v=node['videoRenderer'];vid=v.get('videoId')
                if not vid or vid in seen:continue
                runs=v.get('ownerText',{}).get('runs',[])
                cid=next((x.get('navigationEndpoint',{}).get('browseEndpoint',{}).get('browseId') for x in runs),None)
                if not cid or not re.fullmatch(r'UC[A-Za-z0-9_-]{22}',cid):continue
                video_id('https://www.youtube.com/watch?v='+vid);seen.add(vid)
                out.append({'kind':'video','video_id':vid,'url':'https://www.youtube.com/watch?v='+vid,
                    'channel_id':cid,'channel':text(v.get('ownerText',{})),'title':text(v.get('title',{})),
                    'published_label':text(v.get('publishedTimeText',{})),'published_at':None,
                    'thumbnail':v.get('thumbnail',{}).get('thumbnails',[]),
                    'discovery_locator':response.url+'#videoRenderer/'+vid})
            if len(out)>=count:break
        # A parsed empty result is not an authenticated known-source pool.
        return response,out


def description_chapters(description):
    chapters=[]
    for line in description.splitlines():
        m=re.match(r'^\s*(\d{1,2}:\d{2}(?::\d{2})?)\s+(.+)$',line)
        if not m:continue
        parts=[int(x) for x in m[1].split(':')]
        if any(x>=60 for x in parts[1:]):continue
        seconds=sum(x*60**i for i,x in enumerate(reversed(parts)))
        chapters.append({'start_s':seconds,'title':m[2],'basis':'DESCRIPTION_DECLARED_CHAPTER_NOT_WORD_BOUNDARY'})
    return chapters or None
