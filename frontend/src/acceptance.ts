/** 人工观测必须明确标注，不能从模型输出中推断。 */
export function installAcceptance(target:HTMLElement, context:()=>{session:string;scenario:string}, save:(data:unknown)=>Promise<unknown>) {
 const reviewVersion = 'dialogue-growth-v2';
 const cases = [
  ['D01','司机：主动问候与中文交流'],['D02','司机：政策与当地业务查询'],['D03','司机：记住城市并引导下一步'],['D04','司机：同客户续聊并明确更正'],['D05','司机：口头引导下载和注册'],['D06','司机：未知政策不猜测'],['D07','司机：尊重拒绝并自然收尾'],['D08','司机：停止播放后再次发言'],['D09','司机：工具处理中手动结束通话'],['D10','司机：结束后同客户重新通话'],
  ['R01','骑手：附近站点与配送计酬'],['R02','骑手：没有电瓶车的租赁顾虑'],
  ['M01','商家：入驻条件与成本咨询'],['M02','商家：引导下载商家端和注册'],
  ['F01','度小满：产品说明与使用引导'],['F02','度小满：费用问题有依据'],['F03','度小满：尊重拒绝并停止推进'],['F04','度小满：说明参考方案与政策适用条件'],['F05','度小满：解释注册操作中的困惑'],
 ];
 target.innerHTML=`<summary>人工语音验收记录 <span class="label">每条记录绑定当前会话，未测项保留未测</span></summary><form id="human-review" class="acceptance-form">
 <p class="muted">体验当前语音会话后，可在这里记录观察结果。案例列表供按需选择。客户端传输指标不等于可听延迟；没有实际测量请留空。</p>
 <div class="acceptance-grid"><label>验收案例<select name="case_id">${cases.map(([id,title])=>`<option value="${reviewVersion}-${id}">${reviewVersion}-${id} · ${title}</option>`).join('')}</select></label>
 <label>审核人（昵称）<input name="reviewer" required placeholder="例如：验收人 A"/></label><label>机器与系统<input name="machine" required placeholder="例如：MacBook / macOS"/></label><label>音频设备 / 耳机<input name="audio_device" required placeholder="实际使用设备"/></label><label>网络说明<input name="network" required placeholder="例如：本机 / 家庭 Wi-Fi"/></label>
 <label>实际体验<select name="result" required><option value="unmeasured">未测</option><option value="pass">已观察通过</option><option value="fail">已观察失败</option></select></label><label>回声与卡顿<select name="audio_quality"><option value="unmeasured">未测</option><option value="clear">无可察觉回声或卡顿</option><option value="echo">有回声</option><option value="stutter">有卡顿</option><option value="both">回声和卡顿</option></select></label></div>
 <label>观察记录<textarea name="notes" required rows="3" placeholder="记下听到的回答、查询依据、引导是否清楚及插话体验；不要填真实客户信息。"></textarea></label>
 <label>可选：已校准的可听延迟样本（JSON 数组；没有测量则留空）<textarea name="timing" rows="2" placeholder='[{"sample_id":"reply-01","kind":"first_audio","duration_ms":1200,"method":"calibrated_loopback","evidence_ref":"本地测量记录01","with_tool":false,"valid":true}]'></textarea></label>
 <label><input name="human_attestation" type="checkbox" required/> 我本人已听取并审阅此会话，以上为实际观察。</label>
 <div class="helper"><span>默认不录制原始音频。反馈与当前会话时序一起保存。</span><button class="secondary" type="submit">保存人工记录</button></div><p id="human-review-result" class="muted" role="status"></p></form>`;
 const form=target.querySelector<HTMLFormElement>('form')!;
 const requestedCase=new URLSearchParams(window.location.search).get('voice_case');
 if(cases.some(([id])=>`${reviewVersion}-${id}`===requestedCase))form.querySelector<HTMLSelectElement>('[name="case_id"]')!.value=requestedCase!;
 form.onsubmit=async event=>{
  event.preventDefault(); const output=target.querySelector('#human-review-result')!;
  try{
   const ctx=context();if(!ctx.session)throw new Error('请先完成一个实际语音会话');
   const values=Object.fromEntries(new FormData(form).entries());
   const samples=values.timing?JSON.parse(String(values.timing)):[];
   if(!Array.isArray(samples)||samples.some(s=>!['first_audio','barge_in','stop_button'].includes(s.kind)||typeof s.duration_ms!=='number'||s.duration_ms<0||typeof s.method!=='string'||typeof s.valid!=='boolean'))throw new Error('时序样本格式不正确');
   const record={...values,review_version:reviewVersion,human_attestation:values.human_attestation==='on',timing:samples,scenario_id:ctx.scenario,session_id:ctx.session,browser:navigator.userAgent,platform:navigator.platform,recorded_at:new Date().toISOString(),pipeline:'asr-agent-tts',origin:'human_self_report',raw_audio_saved:false};
   await save(record);output.textContent='人工记录已保存。记录将保留实际观察；可听延迟只有在提供有效测量时才统计。';
  }catch(error){output.textContent=(error as Error).message;}
 };
}
