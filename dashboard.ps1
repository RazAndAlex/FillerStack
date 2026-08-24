# Avvia la dashboard e la apre nel browser. Chiudendo la finestra si spegne tutto.
#
# Cosa accende, in ordine:
#   1. il container Postgres, solo se e' fermo
#   2. l'API vera        python -m uvicorn pipeline.api:app --port 8123
#   3. il server pagine  python dashboard/server_api.py --port 8078
#      (senza database: python dashboard/server_demo.py, dati registrati)
#   4. il browser su http://127.0.0.1:8078/a/
#
# I due processi Python sono legati a questa finestra da un Job Object di
# Windows, quindi muoiono comunque la finestra se ne vada: croce, Ctrl+C o
# taskkill. Il blocco finally aggiunge la parte che il job non copre, cioe'
# spegnere Postgres SE l'ho acceso io. Se era gia' acceso resta acceso: non e'
# roba mia e non la tocco. Chiudendo con la croce il finally non gira, quindi
# in quel caso Postgres resta su anche se l'avevo acceso io. Con Ctrl+C no.

$ErrorActionPreference = 'Stop'

# --- perche' i figli muoiono davvero ----------------------------------------
# Non basta contare sul fatto che condividono la finestra. Provato: uccidendo
# il cmd di colpo, i due Python restavano vivi e le porte occupate. L'unico
# meccanismo che regge in ogni caso e' il Job Object di Windows con
# KILL_ON_JOB_CLOSE: i processi assegnati al job muoiono quando l'ultimo
# riferimento al job si chiude, cioe' quando muore questo processo, comunque
# muoia. Croce, Ctrl+C, taskkill, schermata blu: uguale.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class GruppoProcessi {
    [StructLayout(LayoutKind.Sequential)]
    struct IO_COUNTERS {
        public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
        public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct BASIC_LIMIT {
        public long PerProcessUserTimeLimit, PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass, SchedulingClass;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct EXTENDED_LIMIT {
        public BASIC_LIMIT BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit, JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed, PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    static extern IntPtr CreateJobObject(IntPtr attr, string name);
    [DllImport("kernel32.dll")]
    static extern bool SetInformationJobObject(IntPtr job, int cls, IntPtr info, uint len);
    [DllImport("kernel32.dll")]
    static extern bool AssignProcessToJobObject(IntPtr job, IntPtr proc);

    const uint KILL_ON_JOB_CLOSE = 0x2000;
    const int  EXTENDED_LIMIT_INFORMATION = 9;
    static IntPtr _job = IntPtr.Zero;

    public static bool Crea() {
        _job = CreateJobObject(IntPtr.Zero, null);
        if (_job == IntPtr.Zero) return false;
        EXTENDED_LIMIT info = new EXTENDED_LIMIT();
        info.BasicLimitInformation.LimitFlags = KILL_ON_JOB_CLOSE;
        int len = Marshal.SizeOf(typeof(EXTENDED_LIMIT));
        IntPtr buf = Marshal.AllocHGlobal(len);
        try {
            Marshal.StructureToPtr(info, buf, false);
            return SetInformationJobObject(_job, EXTENDED_LIMIT_INFORMATION, buf, (uint)len);
        } finally { Marshal.FreeHGlobal(buf); }
    }

    public static bool Aggiungi(IntPtr processo) {
        if (_job == IntPtr.Zero) return false;
        return AssignProcessToJobObject(_job, processo);
    }
}
'@

$RADICE        = Split-Path -Parent $MyInvocation.MyCommand.Path
$PORTA_API     = 8123
$PORTA_PAGINE  = 8078
$CONTAINER     = 'plcsim-postgres'
$INDIRIZZO     = "http://127.0.0.1:$PORTA_PAGINE/a/"

Set-Location $RADICE

$avviati           = New-Object System.Collections.ArrayList
$postgresAccesoQui = $false

function Riga($testo, $colore = 'Gray') { Write-Host $testo -ForegroundColor $colore }

function Trova-Python {
    foreach ($c in @('python', 'py')) {
        $g = Get-Command $c -ErrorAction SilentlyContinue
        if ($g) { return $g.Source }
    }
    $noto = 'C:\Program Files\Python314\python.exe'
    if (Test-Path $noto) { return $noto }
    throw "Non trovo Python. Installalo o mettilo nel PATH."
}

function Chi-Occupa($porta) {
    $c = Get-NetTCPConnection -LocalPort $porta -State Listen -ErrorAction SilentlyContinue
    if ($c) { return $c[0].OwningProcess }
    return $null
}

# Una copia vecchia sulla stessa porta e' la trappola nota di questo progetto:
# la pagina prende un 404 e sembra che la route non esista, mentre in realta'
# sta girando codice di ieri. Quindi la fermo, ma solo se e' davvero nostra.
function Libera-Porta($porta, $firma, $etichetta) {
    $idProc = Chi-Occupa $porta
    if (-not $idProc) { return }
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$idProc" -ErrorAction SilentlyContinue
    if ($p -and $p.CommandLine -like "*$firma*") {
        Riga "  porta ${porta}: c'era gia' $etichetta (pid $idProc), lo fermo" 'DarkYellow'
        & taskkill /PID $idProc /T /F 2>&1 | Out-Null
        Start-Sleep -Milliseconds 500
    } else {
        throw "La porta $porta e' occupata dal processo $idProc, che non e' della dashboard. Liberala a mano e riprova."
    }
}

function Aspetta($url, $secondi, $etichetta) {
    $scadenza = (Get-Date).AddSeconds($secondi)
    while ((Get-Date) -lt $scadenza) {
        try {
            $r = Invoke-WebRequest $url -TimeoutSec 2 -UseBasicParsing
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Milliseconds 400
    }
    Riga "  $etichetta non ha risposto entro $secondi secondi" 'Red'
    return $false
}

function Pulisci {
    Riga ''
    Riga 'Spengo.' 'Cyan'
    foreach ($p in $avviati) {
        if ($p -and -not $p.HasExited) {
            & taskkill /PID $p.Id /T /F 2>&1 | Out-Null
            Riga "  fermato pid $($p.Id)"
        }
    }
    if ($postgresAccesoQui) {
        & docker stop $CONTAINER 2>&1 | Out-Null
        Riga "  fermato il container $CONTAINER (l'avevo acceso io)"
    }
    Riga 'Non resta niente acceso.' 'Green'
}

function Adotta($processo, $etichetta) {
    if (-not [GruppoProcessi]::Aggiungi($processo.Handle)) {
        Riga "  attenzione: non sono riuscito a legare $etichetta a questa finestra." 'DarkYellow'
        Riga '  Se la chiudi di colpo potrebbe restare acceso. Usa Ctrl+C.' 'DarkYellow'
    }
}

try {
    $python = Trova-Python
    Riga ''
    Riga '  DASHBOARD RIEMPITRICE' 'White'
    Riga '  ---------------------' 'DarkGray'
    Riga ''

    if (-not [GruppoProcessi]::Crea()) {
        Riga '  attenzione: Windows non mi ha dato un gruppo di processi.' 'DarkYellow'
        Riga '  Chiudi con Ctrl+C per essere sicuro che si spenga tutto.' 'DarkYellow'
    }

    # --- 1. dati vivi o dati registrati? ------------------------------------
    #
    # La dashboard sa girare in due modi. Con PostgreSQL acceso legge l'API
    # vera e mostra lo stato di adesso. Senza, serve la fotografia registrata
    # in dashboard/demo: dati veri, fermi, e nessuna installazione richiesta.
    # Chi scarica il progetto deve poterla aprire comunque.
    $vivo = $false
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        $stato = (& docker inspect -f '{{.State.Running}}' $CONTAINER 2>$null)
        if ($LASTEXITCODE -eq 0) {
            $vivo = $true
            if ($stato -ne 'true') {
                Riga "  accendo il database ($CONTAINER)"
                & docker start $CONTAINER | Out-Null
                $postgresAccesoQui = $true
                Start-Sleep -Seconds 3
            } else {
                Riga "  database gia' acceso, lo lascio com'e'"
            }
        }
    }
    if (-not $vivo) {
        Riga '  niente database: apro i dati registrati' 'DarkYellow'
    }

    # --- 2. l'API vera, solo se ha senso ------------------------------------
    $api = $null
    if ($vivo) {
        Libera-Porta $PORTA_API 'uvicorn pipeline.api' "l'API"
        Riga "  avvio l'API sulla porta $PORTA_API"
        $api = Start-Process -FilePath $python `
            -ArgumentList '-m', 'uvicorn', 'pipeline.api:app', '--port', $PORTA_API `
            -NoNewWindow -PassThru
        [void]$avviati.Add($api)
        Adotta $api "l'API"

        if (Aspetta "http://127.0.0.1:$PORTA_API/health" 60 "l'API") {
            Riga '  API pronta' 'Green'
        } else {
            # Non e' un motivo per fermarsi: i dati registrati bastano a
            # vedere la dashboard, e dirlo e' meglio che morire con un errore.
            Riga "  l'API non si e' alzata: passo ai dati registrati" 'DarkYellow'
            if (-not $api.HasExited) { & taskkill /PID $api.Id /T /F 2>&1 | Out-Null }
            $api = $null
            $vivo = $false
        }
    }

    # --- 3. il server delle pagine ------------------------------------------
    $script = if ($vivo) { 'dashboard/server_api.py' } else { 'dashboard/server_demo.py' }
    $firma  = if ($vivo) { 'server_api.py' } else { 'server_demo.py' }
    Libera-Porta $PORTA_PAGINE $firma 'il server delle pagine'

    Riga "  avvio il server delle pagine sulla porta $PORTA_PAGINE"
    $pagine = Start-Process -FilePath $python `
        -ArgumentList $script, '--port', $PORTA_PAGINE `
        -NoNewWindow -PassThru
    [void]$avviati.Add($pagine)
    Adotta $pagine 'il server delle pagine'

    if (-not (Aspetta "http://127.0.0.1:$PORTA_PAGINE/a/" 30 'il server delle pagine')) {
        throw "Il server delle pagine non si e' alzato."
    }
    Riga '  pagine pronte' 'Green'

    # --- 4. il browser ------------------------------------------------------
    Riga ''
    Riga "  apro $INDIRIZZO" 'Cyan'
    Start-Process $INDIRIZZO

    Riga ''
    Riga '  Le cinque pagine:' 'White'
    Riga "    macchina   http://127.0.0.1:$PORTA_PAGINE/a/"
    Riga "    valvole    http://127.0.0.1:$PORTA_PAGINE/v1/"
    Riga "    rendimento http://127.0.0.1:$PORTA_PAGINE/oee/"
    Riga "    tempo      http://127.0.0.1:$PORTA_PAGINE/pc/"
    Riga "    carte      http://127.0.0.1:$PORTA_PAGINE/k1/"
    Riga ''
    Riga '  Per chiudere: Ctrl+C, oppure la croce di questa finestra.' 'Yellow'
    Riga '  Finche resta aperta, la dashboard e viva.' 'DarkGray'
    Riga ''

    while ($true) {
        Start-Sleep -Seconds 1
        if ($api -and $api.HasExited) { Riga "  l'API si e' fermata da sola" 'Red'; break }
        if ($pagine.HasExited) { Riga '  il server delle pagine si e fermato da solo' 'Red'; break }
    }
}
catch {
    Riga ''
    Riga "ERRORE: $($_.Exception.Message)" 'Red'
    Riga ''
}
finally {
    Pulisci
    Riga ''
    Riga 'Premi un tasto per chiudere.' 'DarkGray'
    try { [void]$Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown') } catch { Start-Sleep -Seconds 3 }
}
