(function(){
  const form=document.getElementById('scanForm');
  const input=document.getElementById('scanInput');
  const bulkCheck=document.getElementById('bulkCheck');
  const bulkMode=document.getElementById('bulkMode');
  const bulkQuantity=document.getElementById('bulkQuantity');
  const bulkStatus=document.getElementById('bulkStatus');
  const state=document.getElementById('scanState');
  if(!form||!input)return;

  const remaining=Number(document.body.dataset.remaining||'0') || 0;
  const focusScan=()=>{try{input.focus({preventScroll:true});}catch(e){input.focus();}};

  function setBulk(on, qty){
    if(bulkMode) bulkMode.value=on?'1':'0';
    if(bulkQuantity) bulkQuantity.value=String(on?qty:1);
    if(bulkStatus) bulkStatus.textContent=on ? ('ON · '+qty+' UNITS') : 'OFF · 1 UNIT';
    if(state) state.textContent=on ? ('BULK READY · SCAN SKU + ENTER · '+qty+' UNITS') : 'READY · PRESS DT50X SCAN TRIGGER';
  }

  if(bulkCheck){
    bulkCheck.addEventListener('change',function(){
      if(!this.checked){ setBulk(false,1); focusScan(); return; }
      const max=remaining || 1;
      const answer=window.prompt('BULK SCAN\n\nEnter quantity to pick for this SKU:\nMaximum: '+max, String(max));
      if(answer===null){ this.checked=false; setBulk(false,1); focusScan(); return; }
      const qty=parseInt(String(answer).trim(),10);
      if(!Number.isFinite(qty)||qty<1||qty>max){
        window.alert('Please enter a quantity from 1 to '+max+'.');
        this.checked=false; setBulk(false,1); focusScan(); return;
      }
      setBulk(true,qty);
      focusScan();
    });
  }

  let timer=null;
  function submitScan(){
    const v=(input.value||'').trim();
    if(!v)return;
    clearTimeout(timer);
    if(state) state.textContent=(bulkCheck&&bulkCheck.checked?'BULK SCAN':'SCAN')+' · SAVING...';
    form.submit();
  }

  input.addEventListener('keydown',function(e){
    if(e.key==='Enter'||e.key==='NumpadEnter'){
      e.preventDefault();
      submitScan();
    }
  });
  input.addEventListener('input',function(){
    if(state) state.textContent=(bulkCheck&&bulkCheck.checked?'BULK SCANNING...':'SCANNING...');
    clearTimeout(timer);
    // DT50X keyboard-wedge fallback: if a scanner does not append ENTER,
    // submit after the barcode has arrived and stopped changing.
    const v=(input.value||'').trim();
    if(v.length>=8) timer=setTimeout(submitScan,300);
  });

  // If the DT50X temporarily sends keystrokes while focus is elsewhere,
  // collect a barcode-like keyboard stream and put it into the real field.
  let buffer='', bufferTimer=null;
  document.addEventListener('keydown',function(e){
    if(e.ctrlKey||e.altKey||e.metaKey)return;
    if(e.key==='Enter'||e.key==='NumpadEnter'){
      if(document.activeElement!==input && buffer.length>=6){
        input.value=buffer; buffer=''; clearTimeout(bufferTimer); submitScan();
      }
      return;
    }
    if(document.activeElement===input)return;
    if(e.key && e.key.length===1 && /[0-9A-Za-z._\/-]/.test(e.key)){
      buffer+=e.key;
      clearTimeout(bufferTimer);
      bufferTimer=setTimeout(()=>buffer='',450);
    }
  },true);

  setBulk(false,1);
  window.addEventListener('load',focusScan);
  window.addEventListener('pageshow',focusScan);
  focusScan();
})();
