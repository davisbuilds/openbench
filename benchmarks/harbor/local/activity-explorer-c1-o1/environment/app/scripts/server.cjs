const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve('web');
const types = {'.html':'text/html','.js':'text/javascript','.css':'text/css','.json':'application/json'};
http.createServer((req,res) => {
  if (req.url === '/api/activities') {
    res.setHeader('Content-Type','application/json');
    return res.end(JSON.stringify({activities: Array.from({length:12}, (_,i) => ({
      id:`activity-${i}`, title:i===3?'Investigate an unexpectedly long activity title with details that should wrap naturally across narrow screens':`Activity ${i+1}`,
      project:['Observatory','Ledger','Workspace'][i%3],status:['running','completed','failed'][i%3],
      description:`Public example ${i+1}.\nInspect the UI, then improve the implementation.`
    }))}));
  }
  let name;
  try { name=decodeURIComponent(new URL(req.url,'http://localhost').pathname); } catch {res.writeHead(400);return res.end();}
  const file=path.resolve(root,'.'+(name==='/'?'/index.html':name));
  if (!file.startsWith(root+path.sep)) {res.writeHead(403);return res.end();}
  fs.readFile(file,(err,data)=>{if(err){res.writeHead(404);return res.end();}res.setHeader('Content-Type',types[path.extname(file)]||'application/octet-stream');res.end(data);});
}).listen(4173,'127.0.0.1',()=>console.log('Activity explorer at http://127.0.0.1:4173'));
