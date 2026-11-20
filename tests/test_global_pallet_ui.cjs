const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'../app/templates/index.html'),'utf8');
const code=html.slice(html.indexOf('function app() {')).split('{% endraw %}')[0];const ctx={URLSearchParams,Intl,Date,console};vm.createContext(ctx);vm.runInContext(code,ctx);
const state=ctx.app();
const make=(id,qty,locations,missing=0)=>({id,parts:[{part_code:'P',qty,pick_locations:locations,pick_missing:missing}]});
const data=items=>({pallet_no:'T',pallet_locations:[],signature:'x',active_items:items,ready_items:items,pick_parts:[{part_code:'P',part_name:'part',qty:items.reduce((s,i)=>s+i.parts[0].qty,0)}]});
(async()=>{
 state.palletPickData=data([make(1,2,[{location:'B1',qty:2}]),make(2,1,[{location:'E1',qty:1}])]);state.palletPickSelected=[1,2];assert.equal(state.palletPickIncomplete,false);assert.equal(state.palletEPickLines.reduce((s,l)=>s+l.qty,0),3);assert(state.palletEPickLines.some(l=>l.location==='B1'));
 state.palletPickSelected=[2];assert.equal(state.palletEPickLines.length,1);assert.equal(state.palletEPickLines[0].location,'E1');state.palletPickSelected=[];assert.equal(state.palletEPickLines.length,0);
 state.palletPickData=data([make(1,2,[],2)]);state.palletPickSelected=[1];assert.equal(state.palletPickIncomplete,true);let calls=0;state.api=async()=>{calls++;throw Error('must not fetch');};await state.printPalletPick('e');assert.equal(calls,0);
 state.palletPickData=data([make(1,2,[{location:'B1',qty:1}])]);assert.equal(state.palletPickIncomplete,true);
 state.palletPickData=data([{id:1,parts:[{part_code:'P',qty:1,pick_locations:[{location:'B',qty:1}]},{part_code:'P',qty:2,pick_locations:[{location:'B',qty:2}]}]}]);assert.equal(state.palletEPickLines[0].qty,3);assert.equal(state.palletPickIncomplete,false);
 state.palletPickData=data([make(1,1,[{location:'B',qty:1}])]);state.palletPickSelected=[1];state.api=async()=>({...state.palletPickData,signature:'CHANGED'});let prints=0;ctx.window={print(){prints++;}};state.$nextTick=async()=>{};await state.printPalletPick('e');assert.equal(prints,0);assert(state.palletPickError.includes('数据或 READY 状态已变化'));
 state.palletPickError='';state.api=async()=>state.palletPickData;await state.printPalletPick('e');assert.equal(prints,1);
 // Actual UI invokes the same getter for screen and print; revalidate signature before print.
 assert(html.includes('打印主仓拣货单'));assert(!html.includes('只列库存仓位以 E 开头'));assert(!html.includes('/^E/i.test(String(loc.location'));
 if(process.argv[2]){
  const payloads=JSON.parse(fs.readFileSync(process.argv[2]));const totals=new Map(),caps=new Map();let demand=0,printed=0,rows=0,items=0,nonE=0;
  for(const p of payloads){state.palletPickData=p;state.palletPickSelected=p.ready_items.map(i=>i.id);items+=p.ready_items.length;assert.equal(state.palletPickIncomplete,false,p.pallet_no);const needs=new Map();for(const i of p.ready_items)for(const x of i.parts)needs.set(x.part_code,(needs.get(x.part_code)||0)+x.qty);const got=new Map();for(const l of state.palletEPickLines){got.set(l.part_code,(got.get(l.part_code)||0)+l.qty);printed+=l.qty;rows++;if(!/^E/i.test(l.location))nonE+=l.qty;const key=l.part_code+'|'+l.location;totals.set(key,(totals.get(key)||0)+l.qty);}assert.deepEqual(got,needs);for(const q of needs.values())demand+=q;for(const part of p.pick_parts)for(const l of part.locations)caps.set(part.part_code+'|'+l.location,l.qty);
   // Every individual selection uses exactly its assigned quantities (no redistribution).
   for(const i of p.ready_items){state.palletPickSelected=[i.id];assert.equal(state.palletPickIncomplete,false);assert.equal(state.palletEPickLines.reduce((s,l)=>s+l.qty,0),i.parts.reduce((s,p)=>s+p.qty,0));}
  }
  let over=0;for(const [k,n] of totals){assert(n<=caps.get(k),'global capacity '+k+':'+n+'>'+caps.get(k));if(n>caps.get(k))over++;}
  const report={status:'PASS',pallets:payloads.length,ready_items:items,demand,printed,omitted:demand-printed,location_overallocated:over,print_rows:rows,non_e_print_qty:nonE};console.log(JSON.stringify(report));
 }
 console.log('PASS: whole app JS, Main nonE/mixed, selections, duplicate parts, shortage fail-closed, print guard, full global capacities');
})().catch(e=>{console.error(e);process.exitCode=1;});
