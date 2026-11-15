/* Explicit source transcription review. It never edits a questionnaire answer. */
async function openSourceTextReview(taskId) {
  await flushEditorSaves();
  const data=await api(`/tasks/${taskId}/source-text-review`);
  const dialog=document.createElement('dialog');dialog.className='source-text-review';
  const changes=new Map(), pages=[...new Set(data.blocks.map(b=>b.location.page))].sort((a,b)=>a-b);
  let page=pages[0],busy=false;
  dialog.innerHTML=`<header><div><h2>原页文字校对：${esc(data.filename)}</h2><p>对照原图纠正读取错字。保留原提取与旧版本，重新分析结构，不会直接入库或覆盖人工答案。</p></div><button type="button" data-close-review>关闭</button></header>${pages.length?`<div class="review-controls"><label>原文页码 <select data-source-page>${pages.map(p=>`<option value="${p}">第 ${p} 页</option>`).join('')}</select></label><span data-edit-count>尚未修改</span></div><div class="source-review-layout"><figure><img alt="原始 PDF 页面" data-review-image><figcaption data-review-caption></figcaption></figure><div data-source-text-list></div></div><footer><label>核对说明<input data-review-note placeholder="例如：已逐字对照原页，纠正 OCR 错字"></label><label class="review-confirm"><input type="checkbox" data-reviewed-source> 已对照原页，只纠正读取文字，不补写答案</label><button class="primary" data-save-source-review disabled>保存校对并重新分析</button><p data-review-status role="status"></p></footer>`:'<p>此文件没有可校对的 OCR 文字。原生 Word/PDF 内容请通过问卷结构与填写位置入口核对。</p>'}`;
  document.body.append(dialog);dialog.showModal();
  const q=s=>dialog.querySelector(s);
  q('[data-close-review]').onclick=()=>dialog.close();dialog.addEventListener('close',()=>dialog.remove());
  if((data.choices||[]).length){const button=document.createElement('button');button.textContent='核对原图勾选状态';dialog.querySelector('header').append(button);button.onclick=()=>{if(changes.size){q('[data-review-status]').textContent='请先保存当前文字校对，或关闭后重新打开；未提交的文字不会自动保存。';return}dialog.close();openSourceChoiceReview(taskId).catch(e=>message(e.message,true))}}
  if(!pages.length)return;
  function update(){q('[data-edit-count]').textContent=`已修改 ${changes.size} 个原文块`;q('[data-save-source-review]').disabled=busy||!changes.size||!q('[data-reviewed-source]').checked||!q('[data-review-note]').value.trim()}
  function renderPage(){
    q('[data-review-image]').src=`/api/tasks/${encodeURIComponent(taskId)}/source-page/${page}.png`;
    q('[data-review-caption]').textContent=`原始 PDF 第 ${page} 页（原文件只读）`;
    q('[data-source-text-list]').innerHTML=data.blocks.filter(b=>b.location.page===page).map((b,i)=>`<article><label>原文块 ${i+1}<small>位置：${b.location.bbox.map(x=>Math.round(x)).join('，')}</small><textarea rows="3" data-source-block="${esc(b.block_id)}">${esc(changes.has(b.block_id)?changes.get(b.block_id):b.text)}</textarea></label><details><summary>查看最初读取文字</summary><pre>${esc(b.original_extracted_text)}</pre></details></article>`).join('');
    dialog.querySelectorAll('[data-source-block]').forEach(el=>el.oninput=()=>{const block=data.blocks.find(b=>b.block_id===el.dataset.sourceBlock);if(el.value===block.text)changes.delete(block.block_id);else changes.set(block.block_id,el.value);update()});
  }
  q('[data-source-page]').onchange=e=>{page=Number(e.target.value);renderPage()};
  q('[data-reviewed-source]').onchange=update;q('[data-review-note]').oninput=update;
  q('[data-review-image]').onerror=()=>{q('[data-review-status]').textContent='原页读取失败，请重试或下载原文件核对。';q('[data-reviewed-source]').checked=false;update()};
  q('[data-save-source-review]').onclick=async()=>{
    busy=true;update();q('[data-review-status]').textContent='正在保存原文校对…';
    try{
      const edits=[...changes].map(([block_id,after])=>({block_id,before:data.blocks.find(b=>b.block_id===block_id).text,after}));
      await api(`/tasks/${taskId}/source-text-review`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({base_version:data.base_version,source_hash:data.source_hash,confirmed_against_source:true,note:q('[data-review-note]').value,edits})});
      q('[data-review-status]').textContent='校对已保存；后台正在重新分析。原件、旧结果及人工答案均保留。';changes.clear();
      message('原页文字校对已保存，后台重新分析完成后可查看新版本。');
    }catch(e){q('[data-review-status]').textContent=`保存失败：${e.message}`}
    finally{busy=false;update()}
  };
  renderPage();
}
document.addEventListener('click',e=>{const b=e.target.closest('[data-open-source-review]');if(b)openSourceTextReview(b.dataset.openSourceReview).catch(err=>message(err.message,true))});

async function openSourceChoiceReview(taskId){
  const data=await api(`/tasks/${taskId}/source-text-review`),choices=data.choices||[];
  if(!choices.length)return message('未找到有物理位置的扫描选框；不能根据文字猜选中状态。',true);
  const dialog=document.createElement('dialog');dialog.className='source-text-review';
  const pages=[...new Set(choices.map(c=>c.location.page))].sort((a,b)=>a-b),changes=new Map();
  let page=pages[0],busy=false;
  const identity=c=>`${c.block_id}|${c.control_index}`;
  dialog.innerHTML=`<header><div><h2>原页勾选校对：${esc(data.filename)}</h2><p>只记录原文件实际勾选，不替本次问卷作答；原选框和旧版本保留，之后重新校验问答关联。</p></div><button data-close-choice-review>关闭</button></header><div class="review-controls"><label>原文页码 <select data-choice-page>${pages.map(p=>`<option value="${p}">第 ${p} 页</option>`).join('')}</select></label><span data-choice-count></span></div><div class="source-review-layout"><figure><img data-choice-image alt="原始PDF选框所在页"><figcaption data-choice-caption></figcaption><button data-full-choice-page>查看完整原页</button></figure><div data-choice-list></div></div><footer><label>核对说明<input data-choice-note></label><label class="review-confirm"><input type="checkbox" data-choice-confirm> 已对照原图确认这些选框的实际状态，不推测未画出的勾选</label><button class="primary" data-save-choice-review disabled>保存勾选校对并重新分析</button><p data-choice-status role="status"></p></footer>`;
  document.body.append(dialog);dialog.showModal();const q=s=>dialog.querySelector(s);
  q('[data-close-choice-review]').onclick=()=>dialog.close();dialog.addEventListener('close',()=>dialog.remove());
  function update(){q('[data-choice-count]').textContent=`已核对修改 ${changes.size} 个选框`;q('[data-save-choice-review]').disabled=busy||!changes.size||!q('[data-choice-note]').value.trim()||!q('[data-choice-confirm]').checked}
  function fullPage(){q('[data-choice-image]').src=`/api/tasks/${encodeURIComponent(taskId)}/source-page/${page}.png`;q('[data-choice-caption]').textContent=`原始 PDF 第 ${page} 页`}
  function render(){fullPage();q('[data-choice-list]').innerHTML=choices.filter(c=>c.location.page===page).map((c,i)=>{const key=identity(c),selected=changes.has(key)?changes.get(key):c.selected;return `<article><b>选框 ${i+1} · ${c.selected===null?'原读取状态不确定':c.selected?'原读取为已选':'原读取为未选'}</b>${(c.field_options||[]).map(([label,option])=>`<p>${esc(label)}<br>选项：${esc(option)}</p>`).join('')||'<p>所属问题关系尚未确定，只核对原图。</p>'}<small>位置：${c.location.bbox.map(Math.round).join('，')}</small><button data-view-choice-region="${esc(c.block_id)}">查看此选框原图特写</button><label>原图实际状态<select data-review-control="${esc(key)}"><option value="unknown" ${selected===null?'selected':''}>尚未确定</option><option value="true" ${selected===true?'selected':''}>原图已勾选</option><option value="false" ${selected===false?'selected':''}>原图未勾选</option></select></label></article>`}).join('');
    dialog.querySelectorAll('[data-review-control]').forEach(s=>s.onchange=()=>{const c=choices.find(v=>identity(v)===s.dataset.reviewControl),value=s.value==='unknown'?null:s.value==='true';if(value===null||value===c.selected)changes.delete(identity(c));else changes.set(identity(c),value);update()});
    dialog.querySelectorAll('[data-view-choice-region]').forEach(b=>b.onclick=()=>{q('[data-choice-image]').src=`/api/tasks/${encodeURIComponent(taskId)}/source-region/${encodeURIComponent(b.dataset.viewChoiceRegion)}.png?version=${data.base_version}`;q('[data-choice-caption]').textContent=`原始 PDF 第 ${page} 页的实际选框特写`;dialog.scrollTop=0});update();
  }
  q('[data-choice-page]').onchange=e=>{page=Number(e.target.value);render()};q('[data-full-choice-page]').onclick=fullPage;
  q('[data-choice-note]').oninput=update;q('[data-choice-confirm]').onchange=update;
  q('[data-choice-image]').onerror=()=>{q('[data-choice-status]').textContent='原页图像读取失败，请重试后再确认。';q('[data-choice-confirm]').checked=false;update()};
  q('[data-save-choice-review]').onclick=async()=>{busy=true;update();q('[data-choice-status]').textContent='正在保存…';try{
    const edits=[...changes].map(([key,after])=>{const c=choices.find(v=>identity(v)===key);return {block_id:c.block_id,control_index:c.control_index,before:c.selected,after}});
    await api(`/tasks/${taskId}/source-choice-review`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({base_version:data.base_version,source_hash:data.source_hash,note:q('[data-choice-note]').value,confirmed_against_source:true,edits})});
    changes.clear();q('[data-choice-status]').textContent='原图勾选校对已保存，正在后台重新校验关联；未授权知识库复用。';
  }catch(e){q('[data-choice-status]').textContent=`保存失败：${e.message}`}finally{busy=false;update()}};render();
}
