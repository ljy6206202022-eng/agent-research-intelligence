"""Bounded public document acquisition. No credentials, scripts or repository execution."""
from __future__ import annotations

from html.parser import HTMLParser
from io import BytesIO
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit

from .public_http import AcquisitionError, PublicHTTP
from .github import GitHub
from agent_research_intelligence.governance.paths import BoundaryError
from agent_research_intelligence.governance.policy import ClassifiedText, require_public


def public_link(base, value):
    url = urljoin(base, value.strip())
    p = urlsplit(url)
    if p.scheme == 'http' and p.hostname in ('arxiv.org', 'export.arxiv.org'):
        url = urlunsplit(('https',p.netloc,p.path,p.query,p.fragment)); p=urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
        return None
    # Discovery is not authorization: PublicHTTP revalidates DNS before fetching.
    return urlunsplit((p.scheme, p.netloc, p.path or '/', p.query, ''))


class HTMLDocument(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.lines, self.links = base, [], []
        self.skip = 0
        self.title, self.in_title, self.canonical = '', False, None
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag=='meta' and a.get('content') and (a.get('name') or a.get('property')):
            self.meta.setdefault((a.get('name') or a.get('property')).lower(),[]).append(a['content'])
        if tag in ('script', 'style', 'noscript', 'template'): self.skip += 1
        if tag == 'title': self.in_title = True
        if tag in ('a', 'link') and a.get('href'):
            link = public_link(self.base, a['href'])
            if link:
                self.links.append({'url': link, 'rel': a.get('rel', ''), 'type': a.get('type', '')})
                if 'canonical' in a.get('rel', '').split(): self.canonical = link

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript', 'template'): self.skip = max(0, self.skip-1)
        if tag == 'title': self.in_title = False

    def handle_data(self, text):
        text = ' '.join(text.split())
        if self.in_title: self.title += text
        if not self.skip and text: self.lines.append(text)


def parse_feed(body, base):
    if re.search(br'<!\s*(DOCTYPE|ENTITY)', body, re.I):
        raise AcquisitionError('XML_DECLARATION_DENIED')
    try: root = ET.fromstring(body)
    except ET.ParseError as exc: raise AcquisitionError('FEED_INVALID') from exc
    entries = []
    def local(e): return e.tag.split('}')[-1]
    for node in root.iter():
        if local(node) not in ('entry', 'item'): continue
        values = {}; links = []; authors = []
        for c in node:
            name = local(c); text = ' '.join(''.join(c.itertext()).split())
            if name == 'link':
                link = public_link(base, c.attrib.get('href') or text)
                if link: links.append(link)
            elif name == 'author': authors.append(text)
            elif name in ('title','summary','description','id','guid','published','updated','pubDate'):
                values[name] = text
        entries.append({**values, 'links': links, 'authors': authors})
        if len(entries) >= 100: break
    return entries


class Documents:
    def __init__(self, http=None, browser=None, guard=None):
        self.http = http or PublicHTTP(timeout=10)
        self.browser = browser
        self.guard = guard or (lambda:None)

    def search_github(self, query: ClassifiedText, count=10):
        require_public(query)
        self.guard()
        url = 'https://api.github.com/search/repositories?' + urlencode({'q':query.text, 'per_page':min(count, 20)})
        r = self.http.get(url); d = json.loads(r.body)
        return r, [{'url': x['html_url'], 'title': x['full_name'],
                    'summary': x.get('description') or '', 'kind':'github',
                    'creator': x['owner']['html_url'], 'api_identity':x['owner']['id'],
                    'discovery_locator':url + '#items/' + str(i)}
                   for i,x in enumerate(d.get('items',[])[:count])]

    def search_papers(self, query: ClassifiedText, count=10):
        require_public(query)
        self.guard()
        url = 'https://export.arxiv.org/api/query?' + urlencode({'search_query':query.text, 'start':0, 'max_results':min(count,20), 'sortBy':'relevance'})
        r = self.http.get(url)
        return r, [{'url':next((u for u in e['links'] if '/abs/' in u), ''),
                    'title':e.get('title',''), 'summary':e.get('summary',''), 'kind':'paper',
                    'authors':e['authors'], 'published_at':e.get('published'),'updated_at':e.get('updated'),
                    'preprint':True,'venue':None,'peer_reviewed':None,'limitations':None,
                    'discovery_locator':url+'#entry/'+str(i)}
                   for i,e in enumerate(parse_feed(r.body, r.url)) if any('/abs/' in u for u in e['links'])]

    def search_crossref(self, query: ClassifiedText, count=10):
        require_public(query)
        self.guard()
        url='https://api.crossref.org/works?'+urlencode({'query':query.text,'rows':min(count,20)})
        r=self.http.get(url);items=json.loads(r.body)['message']['items']
        return r,[{'url':x['URL'],'title':' '.join(x.get('title',[])),
                   'summary':x.get('abstract',''),'kind':'paper','doi':x['DOI'],
                   'authors':x.get('author',[]),'published_at':x.get('published'),'updated_at':x.get('indexed'),
                   'venue':x.get('container-title'),'publication_type':x.get('type'),
                   'preprint':True if x.get('type')=='posted-content' else None,'peer_reviewed':None,
                   'references':x.get('reference'),'limitations':None,'discovery_locator':url+'#items/'+str(i)}
                  for i,x in enumerate(items)]

    def fetch(self, url, kind=None):
        self.guard()
        if urlsplit(url).hostname == 'github.com' and kind == 'github':
            d = GitHub(self.http).research_snapshot(url)
            return {'url':url, 'kind':'github', 'title':d['repository'], 'lines':d['readme'].splitlines(),
                    'locator':d['locator'], 'version':d['commit'], 'license':d['license'],
                    'links':self.markdown_links(d['readme'], d['locator']), 'raw':d['readme'].encode(),
                    'canonical':url, 'metadata':d}
        r = self.http.get(url); mime = r.headers.get('content-type','').split(';')[0]
        common = {'url':r.url,'requested_url':url,'raw':r.body,'version':hashlib.sha256(r.body).hexdigest(),
                  'redirects':r.redirects,'content_type':mime,'locator':r.url,'metadata':{}, 'canonical':r.url}
        if mime == 'application/pdf' or r.body.startswith(b'%PDF-'):
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(r.body)); lines = []
            if len(reader.pages)>100: raise AcquisitionError('PDF_PAGE_LIMIT')
            for n,page in enumerate(reader.pages,1):
                self.guard()
                text = page.extract_text() or ''
                if len(text)>500000: raise AcquisitionError('PDF_TEXT_LIMIT')
                lines.extend(f'[page {n}] {s}' for s in text.splitlines())
            return {**common,'kind':'standard' if kind=='standard' else 'paper','title':str((reader.metadata or {}).get('/Title') or url),'lines':lines,'links':[],
                    'metadata':{'authors':(reader.metadata or {}).get('/Author'),'preprint':None,'venue':None,'peer_reviewed':None,
                        'published_at':None,'limitations':None,'benchmark':None,'experiments':None,
                        'field_status':'PDF metadata assertions; missing fields UNKNOWN; text lines retain original content'}}
        if mime in ('application/atom+xml','application/rss+xml','application/xml','text/xml') or r.body.lstrip().startswith(b'<?xml'):
            entries=parse_feed(r.body,r.url)
            return {**common,'kind':'feed','title':r.url,'entries':entries,
                    'lines':[json.dumps(e,ensure_ascii=False) for e in entries],
                    'links':[{'url':u,'rel':'feed_entry'} for e in entries for u in e['links']]}
        if mime not in ('text/html','application/xhtml+xml','text/plain','text/markdown',''):
            raise AcquisitionError('DOCUMENT_TYPE_UNSUPPORTED')
        if mime in ('text/plain','text/markdown'):
            text=r.body.decode('utf-8',errors='replace')
            return {**common,'kind':kind or 'web','title':r.url,'lines':text.splitlines(),
                    'links':self.markdown_links(text,r.url)}
        p=HTMLDocument(r.url);p.feed(r.body.decode('utf-8',errors='replace'))
        if not p.lines and self.browser is not None:
            rendered=self.browser.fetch(r.url)
            return {**common,'kind':kind or 'web','title':rendered['title'],'lines':rendered['text'].splitlines(),
                    'links':[],'browser_receipt':rendered,'acquisition':'BROWSER_FALLBACK_AFTER_EMPTY_STRUCTURED_TEXT'}
        metadata={'observed_meta':p.meta}
        if kind=='paper' or any(k.startswith('citation_') for k in p.meta):
            metadata.update(authors=p.meta.get('citation_author'),venue=p.meta.get('citation_journal_title') or p.meta.get('citation_conference_title'),
                published_at=p.meta.get('citation_publication_date') or p.meta.get('citation_date'),doi=p.meta.get('citation_doi'),
                preprint=True if (urlsplit(r.url).hostname or '').endswith('arxiv.org') else None,
                peer_reviewed=None,experiments=None,benchmark=None,limitations=None,
                field_status='Publisher metadata assertions, not independently verified peer review; see located text for experiments/limitations')
        if kind=='standard':
            metadata.update(issuer_host=urlsplit(r.url).hostname,standard_status='UNCONFIRMED',
                            revision=common['version'],limitations=['A standard-like document is not automatically normative or current.'])
        return {**common,'kind':kind or 'web','title':p.title or r.url,'lines':p.lines,'metadata':metadata,
                'links':p.links[:500], 'canonical':p.canonical or r.url}

    @staticmethod
    def markdown_links(text, base):
        values = re.findall(r'https://[^\s<>\)\]"\x27]+', text)
        return [{'url':v,'rel':'document_link'} for u in values if (v:=public_link(base,u))][:200]


def related_metadata_sources(candidate):
    """Bounded DOI/ORCID edges from provider metadata; a name is not identity.

    Does not perform extra requests or treat citation count as correctness.
    Followed candidates use the existing discovery budget and connector.
    """
    related=[];edges=[];parent=candidate['url']
    for ref in (candidate.get('references') or [])[:5]:
        doi=ref.get('DOI')
        if not isinstance(doi,str) or not re.fullmatch(r'10\.[0-9]{4,9}/[^\s<>]+',doi):continue
        url='https://doi.org/'+quote(doi,safe='/')
        related.append({'url':url,'title':ref.get('article-title') or doi,'summary':ref.get('unstructured') or '',
                        'kind':'paper','doi':doi,'discovery_locator':candidate.get('discovery_locator',parent),
                        'relation':'CITED_BY_SOURCE_METADATA','relevance':'UNASSESSED'})
        edges.append({'from':parent,'to':url,'relation':'CITES','basis':candidate.get('discovery_locator',parent),'independent_support':False})
    for author in (candidate.get('authors') or [])[:10]:
        if not isinstance(author,dict):
            edges.append({'from':parent,'author_name':str(author),'author_identity':None,'relation':'AUTHOR_NAME_ONLY','identity_status':'UNKNOWN'})
            continue
        identity=author.get('ORCID');affiliations=[a.get('name') for a in author.get('affiliation',[]) if a.get('name')]
        if identity and re.fullmatch(r'https?://orcid\.org/\d{4}-\d{4}-\d{4}-[0-9X]{4}',identity):
            identity='https://'+urlsplit(identity).netloc+urlsplit(identity).path
            related.append({'url':identity,'title':' '.join(filter(None,[author.get('given'),author.get('family')])),
                'summary':'Provider-reported ORCID; author stream is a read-only research candidate.',
                'kind':'web','relation':'AUTHOR_PROFILE','discovery_locator':candidate.get('discovery_locator',parent)})
        else:identity=None
        edges.append({'from':parent,'author_name':' '.join(filter(None,[author.get('given'),author.get('family')])),
            'author_identity':identity,'identity_status':'PROVIDER_ASSERTED_ORCID' if identity else 'UNKNOWN',
            'affiliation_names':affiliations,'affiliation_identity':'UNKNOWN','relation':'AUTHORED_BY','watch':'recommend_only'})
    return related,edges
