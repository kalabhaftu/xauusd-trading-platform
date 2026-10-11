// Attach this READ-ONLY helper to a demo chart for fresh session metadata every minute.
#property strict
input string ExportSymbol = "XAUUSD";
void ExportMetadata()
{
   int handle = FileOpen("xau_sessions.json.tmp", FILE_WRITE | FILE_TXT | FILE_ANSI, 0, CP_UTF8);
   if(handle == INVALID_HANDLE) { Print("Cannot write session export"); return; }
   string text = StringFormat("{\"symbol\":\"%s\",\"server\":\"%s\",\"generated_utc\":%I64d,\"server_offset_seconds\":%I64d,\"sessions\":[",
                             ExportSymbol, AccountInfoString(ACCOUNT_SERVER), (long)TimeGMT(),
                             (long)(TimeTradeServer() - TimeGMT()));
   bool first = true;
   for(int day=0; day<7; day++)
   {
      for(uint index=0; index<20; index++)
      {
         datetime from, to;
         if(!SymbolInfoSessionTrade(ExportSymbol, (ENUM_DAY_OF_WEEK)day, index, from, to)) break;
         if(!first) text += ",";
         text += StringFormat("{\"day\":%d,\"start\":%I64d,\"end\":%I64d}", day, (long)from, (long)to);
         first = false;
      }
   }
   FileWriteString(handle, text + "]}");
   FileClose(handle);
   if(!FileMove("xau_sessions.json.tmp", 0, "xau_sessions.json", FILE_REWRITE))
      Print("Session export publication failed; retry on next timer");
}
int OnInit() { EventSetTimer(60); ExportMetadata(); return INIT_SUCCEEDED; }
void OnTimer() { ExportMetadata(); }
void OnDeinit(const int reason) { EventKillTimer(); }
