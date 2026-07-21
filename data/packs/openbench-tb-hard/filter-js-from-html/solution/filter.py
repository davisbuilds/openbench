#!/usr/bin/env python3
import html, re, sys
p=sys.argv[1]
s=open(p, encoding='utf-8').read()
s=re.sub(r'(?is)<\s*(script|iframe|frame|object|embed)\b[^>]*>.*?<\s*/\s*\1\s*>', '', s)
s=re.sub(r'(?is)<\s*(script|iframe|frame|object|embed)\b[^>]*/?\s*>', '', s)
s=re.sub(r'''(?is)\s+(?:on[a-z0-9_-]+|srcdoc)\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)''', '', s)
attr=re.compile(r'''(?is)(\b(?:href|src|action|formaction|xlink:href|style)\s*=\s*)(["'])(.*?)(\2)''')
def clean(m):
    value=m.group(3)
    normalized=re.sub(r'[\s\x00-\x20]+','',html.unescape(value)).lower()
    if normalized.startswith(('javascript:','vbscript:','data:text/html')) or 'expression(' in normalized or 'url(javascript:' in normalized:
        return m.group(1)+m.group(2)+'#'+m.group(4)
    return m.group(0)
s=attr.sub(clean,s)
s=re.sub(r'''(?is)<meta\b(?=[^>]*http-equiv\s*=\s*["']?refresh)[^>]*(?:javascript|data\s*:\s*text/html)[^>]*>''','',s)
open(p,'w',encoding='utf-8').write(s)
