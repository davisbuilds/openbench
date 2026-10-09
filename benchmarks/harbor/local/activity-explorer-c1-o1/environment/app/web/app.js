fetch('/api/activities').then(r=>r.json()).then(({activities})=>{
  document.querySelector('#state').textContent='';
  for (const activity of activities) {
    const button=document.createElement('button');
    button.textContent=activity.title;
    document.querySelector('#activities').append(button);
  }
});
