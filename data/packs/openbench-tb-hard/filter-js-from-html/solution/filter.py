#!/usr/bin/env python3
import re, sys
p=sys.argv[1]
s=open(p, encoding='utf-8').read()
s=re.sub(r'(?is)<\s*(script|iframe|frame|object|embed)\b[^>]*>.*?<\s*/\s*\1\s*>', '', s)
s=re.sub(r'(?is)<\s*(script|iframe|frame|object|embed)\b[^>]*/?\s*>', '', s)
s=re.sub(r'''(?is)\s+on[a-z0-9_-]+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)''', '', s)
s=re.sub(r'''(?is)(\b(?:href|src|action|formaction|xlink:href)\s*=\s*["']?)\s*(?:java\s*script|vbscript|data\s*:\s*text/html)\s*:''', r'\1', s)
open(p,'w',encoding='utf-8').write(s)
