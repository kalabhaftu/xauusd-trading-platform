// Read-only script: exports sessions and observed server UTC offset. Never submits trades.
#property script_show_inputs
input string ExportSymbol = "XAUUSD";
void OnStart()
{
   int handle = FileOpen("xau_sessions.json", FILE_WRITE | FILE_TXT | FILE_ANSI, 0, CP_UTF8);
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
   Print("Exported read-only session metadata to MQL5/Files/xau_sessions.json; refresh daily.");
}
