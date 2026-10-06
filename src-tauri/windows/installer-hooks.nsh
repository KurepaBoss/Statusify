; Statusify's additions to the Tauri NSIS installer (tauri.conf.json:
; bundle.windows.nsis.installerHooks).
;
; What the uninstaller deliberately does NOT do: it never touches
; %APPDATA%\Statusify, where the user's history, settings and Discord
; Application ID live. (Tauri's own "Delete the application data" box, if
; ticked, clears %APPDATA%\com.statusify.app and %LOCALAPPDATA%\com.statusify.app,
; which only hold the web view's cache; never the Statusify folder.) Whoever
; wants that data gone deletes the folder by hand.
;
; $UpdateMode is 1 when the installer was started with /UPDATE (the in-app
; updater does that), which means "replace the files, change nothing else".

!macro NSIS_HOOK_POSTUNINSTALL
  ${If} $UpdateMode <> 1
    ; "Launch when Windows starts" lives in the user's Run key, not in the
    ; install folder, so removing the program has to remove it too, or Windows
    ; would try to start a program that is gone at every sign-in.
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "StatusifyDesktop"
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run" "StatusifyDesktop"

    ; The remembered install folder (HKCU\Software\<publisher>\<product>). The
    ; stock uninstaller only clears it together with the user's app data, and
    ; this app's data is not in the places it clears, so a plain uninstall
    ; would leave the key behind.
    DeleteRegKey HKCU "${MANUPRODUCTKEY}"
    DeleteRegKey /ifempty HKCU "${MANUKEY}"
  ${EndIf}
!macroend
