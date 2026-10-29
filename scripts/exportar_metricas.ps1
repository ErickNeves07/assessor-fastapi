param(
    [ValidateSet('antes', 'depois')]
    [string]$Etapa = 'antes',
    [string]$BaseUrl = 'http://localhost:8000',
    [string]$Saida = (Join-Path $env:USERPROFILE 'Downloads\metricas_assessor')
)

$ErrorActionPreference = 'Stop'
$painel = Invoke-RestMethod -Uri ($BaseUrl.TrimEnd('/') + '/monitor') -TimeoutSec 10
$turnos = @($painel.turnos)
if ($turnos.Count -eq 0) {
    Write-Output 'Monitor sem turnos. Execute os cenarios no chat e exporte depois.'
    return
}
if ([int]$painel.resumo.em_andamento -gt 0) {
    throw 'Aguarde terminar todas as requisicoes antes de exportar.'
}

$momento = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTimeOffset]::UtcNow, 'E. South America Standard Time')
$coletaDir = Join-Path ([IO.Path]::GetFullPath($Saida)) ($Etapa + '_' + $momento.ToString('yyyyMMdd_HHmmss_fff'))
New-Item -ItemType Directory -Path $coletaDir -Force | Out-Null

$painel | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $coletaDir 'monitor.json') -Encoding UTF8
$turnos | Export-Csv -LiteralPath (Join-Path $coletaDir 'turnos.csv') -NoTypeInformation -Delimiter ';' -Encoding UTF8
@($painel.por_modelo) | Export-Csv -LiteralPath (Join-Path $coletaDir 'modelos.csv') -NoTypeInformation -Delimiter ';' -Encoding UTF8
@($painel.por_rota) | Export-Csv -LiteralPath (Join-Path $coletaDir 'rotas.csv') -NoTypeInformation -Delimiter ';' -Encoding UTF8

$latencias = @($turnos | ForEach-Object { [double]$_.latencia_ms } | Sort-Object)
$indiceMediana = [Math]::Max(0, [Math]::Ceiling(0.5 * $latencias.Count) - 1)
$chamadas = [int](($painel.por_modelo | Measure-Object -Property chamadas -Sum).Sum)
$erros = @($turnos | Where-Object { $_.status -eq 'erro' }).Count
$bloqueados = @($turnos | Where-Object { $_.status -eq 'bloqueado' }).Count
$resumo = [pscustomobject][ordered]@{
    etapa = $Etapa
    coletado_em = $momento.ToString('o')
    fuso = 'America/Sao_Paulo'
    turnos = $turnos.Count
    ok = @($turnos | Where-Object { $_.status -eq 'ok' }).Count
    erros_monitor = $erros
    bloqueados = $bloqueados
    taxa_erro_monitor = $painel.resumo.taxa_erro
    latencia_media_ms = $painel.resumo.latencia_media_ms
    latencia_mediana_nearest_rank_ms = $latencias[$indiceMediana]
    latencia_p95_ms = $painel.resumo.latencia_p95_ms
    latencia_maxima_ms = $latencias[-1]
    slo_p95_limite_ms = $painel.slo.latencia_p95_limite_ms
    slo_taxa_erro_limite = $painel.slo.taxa_erro_limite
    slo_ok = $painel.resumo.slo_ok
    chamadas_modelos_registradas = $chamadas
    chamadas_por_turno = [Math]::Round($chamadas / $turnos.Count, 4)
    tokens_entrada = $painel.resumo.tokens_entrada
    tokens_saida = $painel.resumo.tokens_saida
    tokens_total = $painel.resumo.tokens_total
    tokens_medios_por_turno = [Math]::Round([double]$painel.resumo.tokens_total / $turnos.Count, 2)
    custo_estimado_usd = $painel.resumo.custo_usd
    custo_medio_estimado_usd_por_turno = [Math]::Round([double]$painel.resumo.custo_usd / $turnos.Count, 8)
    fallbacks_registrados = $painel.resumo.fallbacks
    modelo_mais_usado = $painel.resumo.modelo_mais_usado
}
$resumo | Export-Csv -LiteralPath (Join-Path $coletaDir 'resumo.csv') -NoTypeInformation -Delimiter ';' -Encoding UTF8

$ficha = for ($indice = 0; $indice -lt $turnos.Count; $indice++) {
    $turno = $turnos[$indice]
    [pscustomobject][ordered]@{
        sequencia = $indice + 1
        cenario = ''
        marcador = ''
        hora = $turno.hora
        session_id = $turno.session_id
        rota = $turno.rota
        status_monitor = $turno.status
        latencia_ms = $turno.latencia_ms
        tokens_total = $turno.tokens_total
        custo_usd = $turno.custo_usd
        pergunta = ''
        resposta_recebida = ''
        status_http = ''
        rotulo_juiz = ''
        banco_antes = ''
        banco_depois = ''
        resultado_google = ''
        escrita_permaneceu_apos_recusa = ''
        consulta_respeitou_ordem = ''
        falha_tecnica_observada = ''
        observacao = ''
    }
}
$ficha | Export-Csv -LiteralPath (Join-Path $coletaDir 'ficha_resultados.csv') -NoTypeInformation -Delimiter ';' -Encoding UTF8

$contexto = [ordered]@{
    etapa = $Etapa
    coletado_em = $momento.ToString('o')
    origem = $BaseUrl.TrimEnd('/') + '/monitor'
    limite_turnos_no_painel = 50
    custo = 'Estimativa do monitor; nao inclui JEV nem garante custos de chamadas com falha.'
    status = 'Status do monitor nao substitui verificacao do juiz e dos efeitos no banco.'
    amostra = 'Janela do processo atual; anotar separadamente quais cenarios foram executados.'
}
$gitRoot = & git rev-parse --show-toplevel 2>$null
if ($LASTEXITCODE -eq 0 -and $gitRoot) {
    $contexto['commit'] = (& git rev-parse HEAD).Trim()
    $contexto['arquivos_modificados'] = @(& git diff --name-only HEAD)
    $contexto['hashes_arquivos_app'] = @(Get-ChildItem -LiteralPath (Join-Path $gitRoot 'app') -Filter '*.py' -File -Recurse | Get-FileHash -Algorithm SHA256 | Select-Object Path, Hash)
    $juizConteudo = Get-Content -LiteralPath (Join-Path $gitRoot 'app\juiz.py') -Raw
    $juizLimiar = [regex]::Match($juizConteudo, '(?m)^LIMIAR\s*=\s*([0-9.]+)')
    if ($juizLimiar.Success) {
        $contexto['limiar_juiz'] = [double]::Parse($juizLimiar.Groups[1].Value, [Globalization.CultureInfo]::InvariantCulture)
    }
}
$contexto | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $coletaDir 'contexto.json') -Encoding UTF8
Write-Output ('Metricas exportadas em: ' + $coletaDir)
