
const STOCKS=[
"RELIANCE.NS","TCS.NS","INFY.NS","HDFCBANK.NS","ICICIBANK.NS","ITC.NS",
"SBIN.NS","LT.NS","AXISBANK.NS","HINDUNILVR.NS","BAJFINANCE.NS","ASIANPAINT.NS"
]

async function fetchStock(symbol){

const url=`https://query1.finance.yahoo.com/v8/finance/chart/${symbol}?range=1y&interval=1d`

try{
const r=await fetch(url)
const j=await r.json()
return j.chart.result[0].indicators.quote[0].close
}catch(e){
console.log("Fetch error",symbol)
return null
}

}

function ema(data,period){

const k=2/(period+1)
let ema=[]

let sma=data.slice(0,period).reduce((a,b)=>a+b,0)/period
ema[period-1]=sma

for(let i=period;i<data.length;i++){
ema[i]=data[i]*k+ema[i-1]*(1-k)
}

return ema
}

function detectSignals(close){

const ema50=ema(close,50)
const ema200=ema(close,200)

const i=close.length-1
const price=close[i]

if(ema50[i]>ema200[i] && ema50[i-1]<=ema200[i-1])
return {type:"Golden Cross",class:"gc"}

if(ema50[i]<ema200[i] && ema50[i-1]>=ema200[i-1])
return {type:"Death Cross",class:"dc"}

if(price>ema50[i] && ema50[i]>ema200[i])
return {type:"Strong Uptrend",class:"up"}

return null
}

async function scan(){

const tbody=document.querySelector("#results tbody")
tbody.innerHTML=""

for(const s of STOCKS){

const close=await fetchStock(s)
if(!close) continue

const signal=detectSignals(close)
const price=close[close.length-1]

if(signal){

const row=`
<tr>
<td>${s}</td>
<td>${price.toFixed(2)}</td>
<td class="signal ${signal.class}">${signal.type}</td>
</tr>
`

tbody.innerHTML+=row
}

}

}
