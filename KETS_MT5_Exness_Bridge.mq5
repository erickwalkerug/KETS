#property strict
#include <Trade/Trade.mqh>
CTrade trade;
input string KETS_URL="https://kets.onrender.com";
input string KETS_CONNECTOR_TOKEN="PASTE_KETS_CONNECTOR_TOKEN_HERE";
input int PollSeconds=2;
input double DefaultVolume=0.01;
input string GoldSymbol="XAUUSD";
input string BtcSymbol="BTCUSD";
input long MagicNumber=260915;
input bool CloseOppositeOnNewSignal=true;
input int DeviationPoints=30;

string Header(){return "X-KETS-CONNECTOR-TOKEN: "+KETS_CONNECTOR_TOKEN+"\r\nContent-Type: application/json\r\n";}
string GetCommand(){string url=KETS_URL+"/api/mt5/command";char data[],result[];string rh;int code=WebRequest("GET",url,Header(),5000,data,result,rh);if(code!=200)return "";return CharArrayToString(result);}
string JsonEscape(string v){StringReplace(v,"\\","\\\\");StringReplace(v,"\"","\\\"");return v;}
string PositionsJson(){
 string out="["; bool first=true;
 for(int i=0;i<PositionsTotal();i++){
  ulong ticket=PositionGetTicket(i); if(ticket==0)continue;
  if(!PositionSelectByTicket(ticket))continue;
  string sym=PositionGetString(POSITION_SYMBOL);
  long type=PositionGetInteger(POSITION_TYPE);
  double vol=PositionGetDouble(POSITION_VOLUME),open=PositionGetDouble(POSITION_PRICE_OPEN),profit=PositionGetDouble(POSITION_PROFIT),sl=PositionGetDouble(POSITION_SL),tp=PositionGetDouble(POSITION_TP);
  if(!first)out+=","; first=false;
  out+="{\"ticket\":"+IntegerToString((long)ticket)+",\"symbol\":\""+JsonEscape(sym)+"\",\"direction\":\""+(type==POSITION_TYPE_BUY?"BUY":"SELL")+"\",\"volume\":"+DoubleToString(vol,2)+",\"entry\":"+DoubleToString(open,(int)SymbolInfoInteger(sym,SYMBOL_DIGITS))+",\"sl\":"+DoubleToString(sl,(int)SymbolInfoInteger(sym,SYMBOL_DIGITS))+",\"tp\":"+DoubleToString(tp,(int)SymbolInfoInteger(sym,SYMBOL_DIGITS))+",\"profit\":"+DoubleToString(profit,2)+"}";
 }
 out+="]"; return out;
}
bool SendReport(string oid="",string status=""){
 string json="{\"login\":"+IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN))+",\"server\":\""+JsonEscape(AccountInfoString(ACCOUNT_SERVER))+"\",\"balance\":"+DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE),2)+",\"equity\":"+DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY),2)+",\"free_margin\":"+DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE),2)+",\"order_id\":\""+JsonEscape(oid)+"\",\"order_status\":\""+JsonEscape(status)+"\",\"positions\":"+PositionsJson()+"}";
 char data[],result[];StringToCharArray(json,data);char result2[];string rh;int code=WebRequest("POST",KETS_URL+"/api/mt5/report",Header(),5000,data,result2,rh);return code==200;
}
double NormalizeVolume(string symbol,double volume){
 double minv=SymbolInfoDouble(symbol,SYMBOL_VOLUME_MIN),maxv=SymbolInfoDouble(symbol,SYMBOL_VOLUME_MAX),step=SymbolInfoDouble(symbol,SYMBOL_VOLUME_STEP);
 if(minv<=0)minv=0.01;if(maxv<=0)maxv=100; if(step<=0)step=0.01;
 volume=MathMax(minv,MathMin(maxv,volume));
 volume=MathFloor(volume/step+1e-9)*step;
 if(volume<minv)volume=minv;
 int vd=2; if(step<0.01)vd=3; if(step<0.001)vd=4;
 return NormalizeDouble(volume,vd);
}
void CloseOpposite(string symbol,string side){
 if(!CloseOppositeOnNewSignal)return;
 for(int i=PositionsTotal()-1;i>=0;i--){
  ulong ticket=PositionGetTicket(i); if(ticket==0||!PositionSelectByTicket(ticket))continue;
  if(PositionGetString(POSITION_SYMBOL)!=symbol)continue;
  long type=PositionGetInteger(POSITION_TYPE);
  bool opposite=(side=="BUY"&&type==POSITION_TYPE_SELL)||(side=="SELL"&&type==POSITION_TYPE_BUY);
  if(opposite)trade.PositionClose(ticket,DeviationPoints);
 }
}
void ExecuteCommand(string cmd){
 if(StringFind(cmd,"ORDER|")!=0)return;
 string p[];if(StringSplit(cmd,'|',p)<8)return;
 string oid=p[1],symbol=p[2],side=p[3]; if(symbol=="XAUUSD")symbol=GoldSymbol; if(symbol=="BTCUSD")symbol=BtcSymbol;
 double volume=StringToDouble(p[4]);double tp=StringToDouble(p[6]);double sl=StringToDouble(p[7]);if(volume<=0)volume=DefaultVolume;volume=NormalizeVolume(symbol,volume);
 if(!SymbolSelect(symbol,true)){SendReport(oid,"FAILED");return;}
 trade.SetExpertMagicNumber(MagicNumber);trade.SetDeviationInPoints(DeviationPoints);
 CloseOpposite(symbol,side);
 bool ok=false;if(side=="BUY")ok=trade.Buy(volume,symbol,0,sl,tp,"KETS STRONG");else if(side=="SELL")ok=trade.Sell(volume,symbol,0,sl,tp,"KETS STRONG");
 SendReport(oid,ok?"EXECUTED":"FAILED");
}
int OnInit(){EventSetTimer(MathMax(1,PollSeconds));SendReport();return(INIT_SUCCEEDED);}
void OnDeinit(const int r){EventKillTimer();}
void OnTimer(){string c=GetCommand();if(c!=""&&c!="NO_COMMAND")ExecuteCommand(c);SendReport();}
