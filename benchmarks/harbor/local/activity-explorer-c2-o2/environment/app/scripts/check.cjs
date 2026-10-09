const fs = require('node:fs');
for (const file of ['web/index.html', 'web/app.js', 'web/style.css']) {
  if (!fs.statSync(file).isFile()) throw Error(`Missing ${file}`);
}
console.log('Static entry assets present. Verify browser behavior separately.');
