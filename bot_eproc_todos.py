import os
import re
import sys
import json
import time
from datetime import datetime, timedelta

import pyotp
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore

# ============================================================
# SUPORTE A "PULAR ESPERA" APERTANDO ENTER NO TERMINAL
# (não faz nada no GitHub Actions, sem terminal interativo — mas não
# quebra nada, e ajuda quando roda local no VS Code)
# ============================================================
IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    import msvcrt
else:
    import select


def tecla_pular_pressionada():
    try:
        if IS_WINDOWS:
            pulou = False
            while msvcrt.kbhit():
                tecla = msvcrt.getch()
                if tecla in (b"\r", b"\n"):
                    pulou = True
            return pulou
        else:
            if not sys.stdin.isatty():
                return False
            pulou = False
            while select.select([sys.stdin], [], [], 0)[0]:
                linha = sys.stdin.readline()
                if linha is not None:
                    pulou = True
            return pulou
    except Exception:
        return False


def tratar_dialogo(dialog):
    print()
    print("--- ALERTA DA PÁGINA ---")
    print(dialog.message)
    print("------------------------")
    try:
        dialog.accept()
        print("OK clicado automaticamente.")
    except Exception as erro:
        print("Não foi possível clicar no OK:", erro)


class SessaoNavegador:
    def __init__(self, p, headless, arquivo_sessao):
        self.p = p
        self.arquivo_sessao = arquivo_sessao
        self.headless = headless
        self.navegador = None
        self.contexto = None
        self.pagina = None
        self._abrir()

    def _abrir(self):
        self.navegador = self.p.chromium.launch(
            headless=self.headless,
            args=["--disable-http2"],
        )
        if os.path.exists(self.arquivo_sessao):
            self.contexto = self.navegador.new_context(storage_state=self.arquivo_sessao)
        else:
            self.contexto = self.navegador.new_context()
        self.pagina = self.contexto.new_page()
        self.pagina.on("dialog", tratar_dialogo)

    def salvar_sessao(self):
        try:
            self.contexto.storage_state(path=self.arquivo_sessao)
        except Exception as erro:
            print("Aviso ao salvar sessão:", erro)

    def fechar(self):
        try:
            self.navegador.close()
        except Exception:
            pass


# ============================================================
# CONFIGURAÇÕES GERAIS
# ============================================================
load_dotenv()

# IMPORTANTE: sempre visível (headless=False). O bot que comprovadamente
# funciona (TJRJ) roda assim, por trás do Xvfb no GitHub Actions —
# alguns eproc detectam modo headless de verdade e bloqueiam a sessão
# (ex: TJTO retorna 403 em headless). O workflow do GitHub Actions
# deve instalar e usar Xvfb (xvfb-run -a python bot_eproc_todos.py).
HEADLESS = False

ARQUIVO_SESSAO_EPROC = "sessao_eproc_todos.json"
INTERVALO_ENTRE_VARREDURAS = 30
VALOR_MINIMO_CAUSA = float(os.getenv("VALOR_MINIMO_CAUSA_EPROC", "10000"))

CLASSES_EPROC_PADRAO = [
    "Execução de Título Extrajudicial",
    "Busca e Apreensão em Alienação Fiduciária",
    "Monitória",
]

CNPJS_PADRAO_SEM_00 = [
    "60.701.190/0001-04",
    "60.746.948/0001-12",
    "90.400.888/0001-42",
]

# ============================================================
# CONFIGURAÇÃO DE CADA TRIBUNAL
# ============================================================
EPROC_TRIBUNAIS = [
    {
        "nome": "TJRJ",
        "usuario_env": "EPROC_RJ_USUARIO",
        "senha_env": "EPROC_RJ_SENHA",
        "totp_secret_env": "EPROC_RJ_TOTP_SECRET",
        "url_portal": "https://www.tjrj.jus.br/eproc",
        "modo_link": "img_alt",
        "img_alt_padrao": "Eproc 1",
        "url_direto": None,  # sem fallback direto confirmado para o RJ
        "usa_certificado": False,  # sem humano pra clicar em CI — vai direto pra usuário/senha, igual o RJ
        "cnpjs": [
            "00.000.000/0001-91",
            "60.701.190/0001-04",
            "60.746.948/0001-12",
            "90.400.888/0001-42",
        ],
        "classes": CLASSES_EPROC_PADRAO,
        "segundos_espera_geral": 240,
        "timeout_resultados": 150,
    },
    {
        "nome": "TJSC",
        "usuario_env": "EPROC_SC_USUARIO",
        "senha_env": "EPROC_SC_SENHA",
        "totp_secret_env": "EPROC_SC_TOTP_SECRET",
        "url_portal": "https://www.tjsc.jus.br/web/processo-eletronico-eproc",
        "modo_link": "sc",
        "url_direto": "https://eproc1g.tjsc.jus.br/eproc/",
        "texto_link_regex": re.compile(r"Eproc\s+Primeiro\s+Grau", re.IGNORECASE),
        "usa_certificado": False,  # sem humano pra clicar em CI — vai direto pra usuário/senha, igual o RJ
        "cnpjs": CNPJS_PADRAO_SEM_00,
        "classes": CLASSES_EPROC_PADRAO,
        "segundos_espera_geral": 240,
        "timeout_resultados": 150,
    },
    {
        "nome": "TJMG",
        "usuario_env": "EPROC_MG_USUARIO",
        "senha_env": "EPROC_MG_SENHA",
        "totp_secret_env": "EPROC_MG_TOTP_SECRET",
        "url_portal": "https://www.tjmg.jus.br/portal-tjmg/processos/eproc/eproc.htm",
        "modo_link": "mg",
        "url_direto": (
            "https://eproc1g.tjmg.jus.br/eproc/externo_controlador.php"
            "?acao=principal&sigla_orgao_sistema=TJMG&sigla_sistema=Eproc"
        ),
        "texto_link_regex": re.compile(r"eproc\s*1[ªa]?\s*Inst[âa]ncia", re.IGNORECASE),
        "usa_certificado": False,  # sem humano pra clicar em CI — vai direto pra usuário/senha, igual o RJ
        "cnpjs": CNPJS_PADRAO_SEM_00,
        "classes": CLASSES_EPROC_PADRAO,
        "segundos_espera_geral": 240,
        "timeout_resultados": 150,
    },
    {
        "nome": "TJTO",
        "usuario_env": "EPROC_TO_USUARIO",
        "senha_env": "EPROC_TO_SENHA",
        "totp_secret_env": "EPROC_TO_TOTP_SECRET",
        "url_portal": "https://www.tjto.jus.br/eproc",
        "modo_link": "tjto",
        # URL de uso único (state/nonce) — pode falhar com "Cookie not
        # found" se já tiver sido usada antes; só um fallback melhor
        # esforço.
        "url_direto": (
            "https://sso.tjto.jus.br/realms/eproc/protocol/openid-connect/auth"
            "?scope=openid"
            "&state=OO998gA0MLpa_qwfy8FP0Mp1yHOUEjDMMcUbttKZIaE.JPwxReCqwRs.eproc-tjto-1g"
            "&response_type=code"
            "&client_id=cnj.jus.br"
            "&redirect_uri=https%3A%2F%2Fsso.cloud.pje.jus.br%2Fauth%2Frealms%2Fpje%2Fbroker%2Ftjto%2Fendpoint"
            "&eproc_client_id=eproc-1g.tjto.jus.br"
            "&nonce=GqPQwZ7qPLqlamHTZ-Ldbg"
        ),
        "texto_link_regex": re.compile(r"EPROC\s*-\s*1[°ºo]?\s*Grau", re.IGNORECASE),
        "usa_certificado": False,  # TJTO nem tem essa opção mesmo
        "cnpjs": CNPJS_PADRAO_SEM_00,
        "classes": CLASSES_EPROC_PADRAO,
        "segundos_espera_geral": 240,
        "timeout_resultados": 150,
    },
    {
        "nome": "TJAC",
        "usuario_env": "EPROC_AC_USUARIO",
        "senha_env": "EPROC_AC_SENHA",
        "totp_secret_env": "EPROC_AC_TOTP_SECRET",
        "url_portal": "https://portal-eproc.tjac.jus.br/",
        "modo_link": "tjac",
        "prefixo_href_resultado": "https://eproc1g.tjac.jus.br",
        "texto_link_regex": re.compile(r"Clique\s+aqui", re.IGNORECASE),
        # URL de uso único (state/nonce) — mesmo aviso do TJTO.
        "url_direto": (
            "https://sso.tjac.jus.br/realms/eproc/protocol/openid-connect/auth"
            "?scope=openid"
            "&state=Fve0OK3ZVnWNgKNFj3pn9iRKcGNwsnQiIG4TOkWK5go.MKX-ONxsSx8.eproc-tjac-1g"
            "&response_type=code"
            "&client_id=cnj.jus.br"
            "&redirect_uri=https%3A%2F%2Fsso.cloud.pje.jus.br%2Fauth%2Frealms%2Fpje%2Fbroker%2Feproc-tjac%2Fendpoint"
            "&eproc_client_id=eproc1g.tjac.jus.br"
            "&nonce=KJQ1EbbHWvHaPVFod1QKEg"
        ),
        "usa_certificado": False,  # sem humano pra clicar em CI — vai direto pra usuário/senha, igual o RJ
        # CONFIRMADO: TJAC usa "00.000.000/0001-91" no lugar de
        # "60.701.190/0001-04" (diferente dos outros).
        "cnpjs": [
            "00.000.000/0001-91",
            "60.746.948/0001-12",
            "90.400.888/0001-42",
        ],
        "classes": CLASSES_EPROC_PADRAO,
        "segundos_espera_geral": 240,
        "timeout_resultados": 150,
    },
]


def diagnosticar_env_carregado():
    """
    Confirma no terminal que as variáveis de cada tribunal foram
    lidas do .env/secrets, SEM expor os valores reais — ajuda a pegar
    problemas comuns como secret não configurado ou senha cortada por
    um '#' sem aspas.
    """
    def mascarar(valor):
        if not valor:
            return "(vazio ou não definido)"
        if len(valor) <= 4:
            return f"'{valor[0]}...' (tamanho {len(valor)})"
        return f"'{valor[:2]}...{valor[-2:]}' (tamanho {len(valor)})"

    print()
    print("==========================================")
    print(" DIAGNÓSTICO DO .ENV/SECRETS (sem expor os valores)")
    print("==========================================")
    for tribunal in EPROC_TRIBUNAIS:
        usuario = os.getenv(tribunal["usuario_env"])
        senha = os.getenv(tribunal["senha_env"])
        totp = os.getenv(tribunal["totp_secret_env"])
        print(f"--- {tribunal['nome']} ---")
        print(f"  {tribunal['usuario_env']}:", mascarar(usuario))
        print(f"  {tribunal['senha_env']}:", mascarar(senha))
        print(f"  {tribunal['totp_secret_env']}:", mascarar(totp))
    print(
        "Se algum desses parecer curto demais (ex: a senha real tem "
        "mais caracteres que o 'tamanho' mostrado), o valor provavelmente "
        "está sendo cortado — geralmente por causa de um '#' sem aspas "
        'no meio da senha. Ex: EPROC_MG_SENHA="sua#senha!aqui"'
    )
    print("==========================================")


def gerar_codigo_totp(totp_secret):
    if not totp_secret:
        raise RuntimeError(
            "Chave TOTP não definida para este tribunal. É a chave secreta "
            "mostrada ao lado do QR code quando o app autenticador foi "
            "cadastrado."
        )
    return pyotp.TOTP(totp_secret).now()


def navegar_com_retry(pagina, url, tentativas=5, timeout=120000, wait_until="domcontentloaded"):
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        try:
            pagina.goto(url, wait_until=wait_until, timeout=timeout)
            return True
        except Exception as erro:
            ultimo_erro = erro
            print(f"Erro ao navegar para {url} (tentativa {tentativa}/{tentativas}): {erro}")
            if tentativa < tentativas:
                pagina.wait_for_timeout(5000 * tentativa)
    print(f"Falha definitiva ao navegar para {url} após {tentativas} tentativas: {ultimo_erro}")
    return False


def fechar_banner_cookies(pagina):
    try:
        for texto_botao in ["Aceitar", "Concordar", "Ok", "Fechar", "Aceito"]:
            botao_cookie = pagina.get_by_role("button", name=re.compile(texto_botao, re.IGNORECASE))
            if botao_cookie.count() > 0 and botao_cookie.first.is_visible():
                botao_cookie.first.click()
                print(f"Fechei um banner de cookies/aviso clicando em '{texto_botao}'.")
                pagina.wait_for_timeout(500)
                return
    except Exception:
        pass


# ============================================================
# FIREBASE
# ============================================================
NOME_ARQUIVO_FIREBASE = "firebase-service-account-eproc.json"

print()
print("==========================================")
print(" CONECTANDO AO FIREBASE")
print("==========================================")
try:
    credencial = credentials.Certificate(NOME_ARQUIVO_FIREBASE)
    firebase_admin.initialize_app(credencial)
    db = firestore.client()
    print("Firebase conectado com sucesso!")
except Exception as erro:
    print()
    print("==========================================")
    print(" ERRO AO CONECTAR AO FIREBASE")
    print("==========================================")
    print("Tipo:", type(erro).__name__)
    print("Detalhes:", erro)
    raise


def processo_existe_no_firebase(numero):
    if not numero:
        return False
    try:
        documento = db.collection("processos").document(numero).get()
        if documento.exists:
            print(f"[JÁ EXISTE] {numero}")
            return True
        print(f"[NOVO] {numero}")
        return False
    except Exception as erro:
        print("ERRO AO CONSULTAR FIREBASE:", type(erro).__name__, erro)
        return True


def valor_causa_para_float(valor_causa_texto):
    if not valor_causa_texto:
        return None
    numero = re.sub(r"[^\d,.]", "", valor_causa_texto)
    numero = numero.replace(".", "").replace(",", ".")
    try:
        return float(numero)
    except ValueError:
        return None


def formatar_moeda_br(valor):
    texto = f"{valor:,.2f}"
    texto = texto.replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {texto}"


def salvar_processo_no_firebase(dados, tribunal_origem):
    numero = dados.get("numero")
    if not numero:
        print()
        print("ERRO: processo sem número.")
        return False
    dados["tribunal_origem_consulta"] = tribunal_origem
    dados["dataCaptacao"] = datetime.now()
    dados["dataExpiracao"] = dados["dataCaptacao"] + timedelta(days=10)
    dados["emProcessos"] = True
    dados["data_distribuicao"] = dados["dataCaptacao"]

    try:
        db.collection("processos").document(numero).set(dados)
        print()
        print("==========================================")
        print(" PROCESSO SALVO NO FIREBASE")
        print("==========================================")
        print("Número:", dados.get("numero"))
        print("Réu:", dados.get("reu"))
        print("CPF/CNPJ:", dados.get("documento_reu"))
        print("Classe:", dados.get("classe"))
        print("Tribunal:", tribunal_origem)
        print("Autor:", dados.get("autor"))
        print("Valor:", dados.get("valor_causa"))
        return True
    except Exception as erro:
        print()
        print("ERRO AO SALVAR NO FIREBASE:", type(erro).__name__, erro)
        return False


def diagnosticar_tela(pagina, rotulo):
    print()
    print("==========================================")
    print(f" DIAGNÓSTICO — {rotulo}")
    print("==========================================")
    try:
        print("Título da página:", pagina.title())
        print("URL atual:", pagina.url)
    except Exception as erro:
        print("Não consegui pegar título/URL:", erro)
    try:
        os.makedirs("debug_eproc_todos", exist_ok=True)
        agora = datetime.now().strftime("%Y%m%d_%H%M%S")
        nome_arquivo = f"debug_eproc_todos/{rotulo}_{agora}.png".replace(" ", "_")
        pagina.screenshot(path=nome_arquivo, full_page=True)
        print("Screenshot salvo em:", nome_arquivo)
    except Exception as erro:
        print("Não foi possível salvar screenshot:", erro)
    print("==========================================")


# ============================================================
# LOGIN — CERTIFICADO / USUÁRIO+SENHA / 2FA
# ============================================================
SEGUNDOS_ESPERA_CERTIFICADO_MANUAL = 90


def tentar_login_certificado(pagina, nome_tribunal):
    """
    Espera até SEGUNDOS_ESPERA_CERTIFICADO_MANUAL por uma tentativa de
    certificado digital manual (só faz sentido rodando localmente com
    terminal interativo). No GitHub Actions, sem tty, isso passa
    direto (tecla_pular_pressionada sempre False, mas o timeout de 90s
    é curto o suficiente pra não travar muito o ciclo).
    """
    print()
    print("------------------------------------------------")
    print(f" LOGIN VIA CERTIFICADO DIGITAL — {nome_tribunal} (eproc)")
    print("------------------------------------------------")

    url_inicial = pagina.url
    logou_com_certificado = False

    for segundo in range(SEGUNDOS_ESPERA_CERTIFICADO_MANUAL):
        pagina.wait_for_timeout(1000)

        if tecla_pular_pressionada():
            print("ENTER pressionado — seguindo com login por usuário e senha.")
            break

        if pagina.url != url_inicial and "openid-connect/auth" not in pagina.url:
            print("Parece que saiu da tela de login — considerando certificado OK.")
            logou_com_certificado = True
            break

        if (segundo + 1) % 30 == 0:
            restante = SEGUNDOS_ESPERA_CERTIFICADO_MANUAL - (segundo + 1)
            print(f"[{nome_tribunal}] Ainda aguardando certificado... ({restante}s restantes)")
    else:
        print("Tempo esgotado sem ação — seguindo com login por usuário e senha.")

    return logou_com_certificado


def tentar_login_usuario_senha(pagina, contexto, usuario, senha, nome_tribunal):
    if not usuario or not senha:
        print(f"[{nome_tribunal}] ERRO: usuário e/ou senha não definidos para este tribunal.")
        return False, pagina

    def parece_tela_de_login(p):
        try:
            if "eproc-sso" in p.url or "openid-connect" in p.url:
                return True
            if p.get_by_label(re.compile("usu[áa]rio", re.IGNORECASE)).count() > 0:
                return True
        except Exception:
            pass
        return False

    if contexto is not None and not parece_tela_de_login(pagina):
        print(f"[{nome_tribunal}] Aviso: a aba atual não parece ser a tela de login — trocando de aba...")
        if len(contexto.pages) > 1:
            pagina = contexto.pages[-1]
            pagina.on("dialog", tratar_dialogo)
        else:
            for p in contexto.pages:
                if parece_tela_de_login(p):
                    pagina = p
                    pagina.on("dialog", tratar_dialogo)
                    break

    campo_usuario = None
    for seletor in ["#username", "input[name='username']"]:
        loc = pagina.locator(seletor)
        if loc.count() > 0:
            campo_usuario = loc.first
            break
    if campo_usuario is None:
        try:
            loc = pagina.get_by_label(re.compile(r"^\s*usu[áa]rio\s*$", re.IGNORECASE), exact=True)
            if loc.count() > 0:
                campo_usuario = loc.first
        except Exception:
            pass
    if campo_usuario is None:
        loc = pagina.locator("input[type='text']:visible")
        if loc.count() > 0:
            campo_usuario = loc.first

    campo_senha = None
    for seletor in ["#password", "input[name='password']"]:
        loc = pagina.locator(seletor)
        if loc.count() > 0:
            campo_senha = loc.first
            break
    if campo_senha is None:
        try:
            loc = pagina.get_by_label(re.compile(r"^\s*senha\s*$", re.IGNORECASE), exact=True)
            if loc.count() > 0:
                campo_senha = loc.first
        except Exception:
            pass
    if campo_senha is None:
        loc = pagina.locator("input[type='password']:visible")
        if loc.count() > 0:
            campo_senha = loc.first

    if campo_usuario is None or campo_senha is None:
        print(f"[{nome_tribunal}] ERRO: não encontrei os campos de usuário/senha.")
        diagnosticar_tela(pagina, f"{nome_tribunal}_campos_login_nao_encontrados")
        return False, pagina

    try:
        campo_usuario.fill(usuario)
        campo_senha.fill(senha)
    except Exception as erro:
        print(f"[{nome_tribunal}] Erro ao preencher usuário/senha:", erro)
        diagnosticar_tela(pagina, f"{nome_tribunal}_erro_preencher_login")
        return False, pagina

    botao_clicado = False
    for seletor in ["#kc-login", "button[type='submit']", "input[type='submit']"]:
        loc = pagina.locator(seletor)
        if loc.count() > 0 and loc.first.is_visible():
            try:
                loc.first.click()
                botao_clicado = True
                break
            except Exception:
                continue

    if not botao_clicado:
        try:
            botao_entrar = pagina.get_by_role("button", name=re.compile("entrar", re.IGNORECASE))
            if botao_entrar.count() > 0 and botao_entrar.first.is_visible():
                botao_entrar.first.click()
                botao_clicado = True
        except Exception:
            pass

    if not botao_clicado:
        try:
            campo_senha.press("Enter")
        except Exception as erro:
            print(f"[{nome_tribunal}] Erro ao pressionar Enter no campo de senha:", erro)
            return False, pagina

    print(f"[{nome_tribunal}] Usuário e senha enviados.")
    pagina.wait_for_timeout(2000)
    return True, pagina


def _confirmar_totp(pagina, campo_referencia):
    for seletor_botao in ["#kc-login", "button[type='submit']", "input[type='submit']"]:
        loc = pagina.locator(seletor_botao)
        if loc.count() > 0 and loc.first.is_visible():
            try:
                loc.first.click()
                pagina.wait_for_timeout(2000)
                return
            except Exception:
                continue
    try:
        botao_confirmar = pagina.get_by_role(
            "button", name=re.compile("confirmar|validar|verificar|entrar", re.IGNORECASE)
        )
        if botao_confirmar.count() > 0 and botao_confirmar.first.is_visible():
            botao_confirmar.first.click()
            pagina.wait_for_timeout(2000)
            return
    except Exception:
        pass
    try:
        campo_referencia.press("Enter")
    except Exception:
        pass
    pagina.wait_for_timeout(2000)


def _preencher_totp_campo_unico(pagina, campo, totp_secret):
    codigo = gerar_codigo_totp(totp_secret)
    print(f"Tela de 2FA detectada (campo único) — preenchendo código {codigo}...")
    campo.fill(codigo)
    _confirmar_totp(pagina, campo)
    return True


def _preencher_totp_multiplos_campos(pagina, campos_digito, total_digitos, totp_secret):
    codigo = gerar_codigo_totp(totp_secret)
    print(f"Tela de 2FA detectada ({total_digitos} campos de 1 dígito) — preenchendo código {codigo}...")
    for i, digito in enumerate(codigo):
        if i >= total_digitos:
            break
        try:
            campos_digito.nth(i).fill(digito)
        except Exception as erro:
            print(f"Erro ao preencher dígito {i}:", erro)
    ultimo_campo = campos_digito.nth(min(len(codigo), total_digitos) - 1)
    _confirmar_totp(pagina, ultimo_campo)
    return True


def tratar_totp_se_necessario(pagina, totp_secret, nome_tribunal, timeout_segundos=60):
    print()
    print(f"[{nome_tribunal}] Verificando se há tela de 2FA (até {timeout_segundos}s)...")

    seletores_campo_unico = [
        "#txtAcessoCodigo",  # confirmado no TJMG
        "#otp",
        "input[name='otp']",
        "input[name='totp']",
        "input[autocomplete='one-time-code']",
    ]

    for segundo in range(timeout_segundos):
        for seletor in seletores_campo_unico:
            campo = pagina.locator(seletor)
            if campo.count() > 0:
                try:
                    if campo.first.is_visible(timeout=1000):
                        return _preencher_totp_campo_unico(pagina, campo.first, totp_secret)
                except Exception:
                    continue

        try:
            campo_label = pagina.get_by_label(
                re.compile("c[óo]digo|autentica|verifica|totp|otp|token", re.IGNORECASE)
            )
            if campo_label.count() > 0 and campo_label.first.is_visible(timeout=1000):
                return _preencher_totp_campo_unico(pagina, campo_label.first, totp_secret)
        except Exception:
            pass

        try:
            campos_digito = pagina.locator("input[maxlength='1']")
            total_digitos = campos_digito.count()
            if total_digitos >= 6 and campos_digito.first.is_visible(timeout=1000):
                return _preencher_totp_multiplos_campos(pagina, campos_digito, total_digitos, totp_secret)
        except Exception:
            pass

        pagina.wait_for_timeout(1000)
        if tecla_pular_pressionada():
            print("ENTER pressionado — parando de esperar pela tela de 2FA.")
            break

    print(f"[{nome_tribunal}] Nenhuma tela de 2FA detectada dentro do tempo (ou não havia mesmo).")
    try:
        diagnosticar_tela(pagina, f"{nome_tribunal}_2fa_nao_detectado")
    except Exception:
        pass
    return False


# ============================================================
# CONSULTA PROCESSUAL — NAVEGAÇÃO E FORMULÁRIO
# ============================================================
def abrir_consulta_processual(pagina):
    menu_consulta = pagina.get_by_text(re.compile(r"^\s*Consulta Processual\s*$", re.IGNORECASE))
    if menu_consulta.count() == 0:
        menu_consulta = pagina.get_by_text(re.compile("Consulta Processual", re.IGNORECASE))
    if menu_consulta.count() == 0:
        return False

    try:
        menu_consulta.first.click()
    except Exception:
        return False

    pagina.wait_for_timeout(1000)

    submenu_consultar = pagina.get_by_text(re.compile(r"^\s*Consultar Processos\s*$", re.IGNORECASE))
    if submenu_consultar.count() == 0:
        submenu_consultar = pagina.get_by_text(re.compile("Consultar Processos", re.IGNORECASE))
    if submenu_consultar.count() == 0:
        return False

    try:
        submenu_consultar.first.click()
    except Exception:
        return False

    try:
        pagina.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    pagina.wait_for_timeout(1500)

    try:
        campo_numero = pagina.get_by_label(re.compile(r"N[ºo]?\.?\s*Processo", re.IGNORECASE))
        if campo_numero.count() > 0:
            return True
    except Exception:
        pass

    return False


VALORES_TIPO_PESQUISA = {
    "Número de Processo, Chave": "NU",
    "Nome da Parte": "NO",
    "CPF/CNPJ": "CP",
    "OAB": "OA",
    "Originário / Relacionado": "NP",
    "Número CDA / Proc. Adm. CDA.": "CD",
    "IPL": "IP",
}

PADRAO_NUMERO_PROCESSO = re.compile(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}")
PADRAO_NUMERO_PROCESSO_GENERICO = re.compile(r"\d{7}-\d{2}\.\S{3,20}")
PADRAO_DOCUMENTO_PARENTESES = re.compile(
    r"\((\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{3}\.\d{3}\.\d{3}-\d{2})\)"
)


def selecionar_tipo_pesquisa(pagina, texto_opcao):
    campo_tipo = pagina.locator("#selTipoPesquisa")
    if campo_tipo.count() == 0:
        campo_tipo = pagina.locator("select[name='tipoPesquisa']")
    if campo_tipo.count() == 0:
        campo_tipo = pagina.locator("select:visible")
    if campo_tipo.count() == 0:
        return False

    valor = VALORES_TIPO_PESQUISA.get(texto_opcao)
    try:
        if valor:
            campo_tipo.first.select_option(value=valor)
        else:
            campo_tipo.first.select_option(label=texto_opcao)
        return True
    except Exception:
        try:
            campo_tipo.first.select_option(label=texto_opcao)
            return True
        except Exception:
            return False


def preencher_cpf_cnpj(pagina, valor):
    campo = pagina.locator("input[name='strDocParte']:visible")
    if campo.count() == 0:
        campo = pagina.locator("input[name='strDocParte']")
    if campo.count() == 0:
        try:
            loc = pagina.get_by_label(re.compile(r"N[úu]mero do CPF/?CNPJ", re.IGNORECASE))
            if loc.count() > 0:
                campo = loc
        except Exception:
            campo = None
    if campo is None or campo.count() == 0:
        loc = pagina.locator("input[type='text']:visible")
        campo = loc if loc.count() > 0 else None
    if campo is None or campo.count() == 0:
        return False

    alvo = campo.first

    def valor_normalizado(texto):
        return re.sub(r"\D", "", texto or "")

    try:
        alvo.click()
        alvo.fill("")
        alvo.fill(valor)
        pagina.wait_for_timeout(300)

        valor_atual = alvo.input_value()
        if valor_normalizado(valor_atual) != valor_normalizado(valor):
            alvo.click()
            alvo.press("Control+A")
            alvo.press("Backspace")
            alvo.type(valor, delay=50)
            pagina.wait_for_timeout(300)
            valor_atual = alvo.input_value()
            if valor_normalizado(valor_atual) != valor_normalizado(valor):
                return False

        return True
    except Exception:
        return False


def _localizar_botao_classe_processual(pagina):
    botoes_ms = pagina.locator("button.ms-choice")
    total_botoes = botoes_ms.count()
    campo_classe = None

    if total_botoes == 1:
        campo_classe = botoes_ms.first
    elif total_botoes > 1:
        try:
            rotulo = pagina.get_by_text(re.compile(r"^\s*Classe Processual\s*$", re.IGNORECASE))
            if rotulo.count() > 0:
                candidato = rotulo.first.locator(
                    "xpath=following::button[contains(@class,'ms-choice')][1]"
                )
                if candidato.count() > 0:
                    campo_classe = candidato.first
        except Exception:
            pass
        if campo_classe is None:
            campo_classe = botoes_ms.first

    if campo_classe is None:
        try:
            loc = pagina.get_by_label(re.compile("Classe Processual", re.IGNORECASE))
            if loc.count() > 0:
                campo_classe = loc.first
        except Exception:
            pass

    return campo_classe


def _marcar_ou_desmarcar_opcao_classe(pagina, texto_exato, listar_opcoes_se_nao_achar=True):
    try:
        campo_busca = pagina.locator(".ms-drop .ms-search input:visible")
        if campo_busca.count() > 0:
            campo_busca.last.fill("")
            campo_busca.last.fill(texto_exato)
        else:
            campo_busca_generico = pagina.locator("input[type='text']:visible, input[type='search']:visible")
            if campo_busca_generico.count() > 0:
                campo_busca_generico.last.fill("")
                campo_busca_generico.last.fill(texto_exato)
    except Exception:
        pass
    pagina.wait_for_timeout(1000)

    padrao_exato = re.compile(r"^\s*" + re.escape(texto_exato) + r"\s*$", re.IGNORECASE)
    opcao = pagina.locator(".ms-drop label").filter(has_text=padrao_exato)
    if opcao.count() == 0:
        opcao = pagina.get_by_text(padrao_exato, exact=True)
    if opcao.count() == 0:
        opcao = pagina.get_by_text(padrao_exato)

    if opcao.count() == 0:
        if listar_opcoes_se_nao_achar:
            try:
                opcoes_visiveis = pagina.locator(".ms-drop label:visible")
                total = opcoes_visiveis.count()
                print(f"Opções visíveis com esse filtro de busca ({total}):")
                for i in range(min(total, 20)):
                    try:
                        print("  -", opcoes_visiveis.nth(i).inner_text().strip())
                    except Exception:
                        continue
            except Exception:
                pass
        return False

    try:
        opcao.first.click()
        return True
    except Exception:
        return False


def definir_classe_unica(pagina, texto_marcar):
    """
    Abre o combobox de Classe Processual e desmarca TUDO que já
    estiver marcado (não confia em rastrear "a última classe usada" —
    isso pode dessincronizar) antes de marcar a classe desejada.
    Garante sempre exatamente 1 classe marcada.
    """
    campo_classe = _localizar_botao_classe_processual(pagina)
    if campo_classe is None:
        print("ERRO: não encontrei o botão de 'Classe Processual' (button.ms-choice).")
        return False

    try:
        campo_classe.click()
    except Exception as erro:
        print("Erro ao abrir 'Classe Processual':", erro)
        return False

    pagina.wait_for_timeout(800)

    total_desmarcados = 0
    for _ in range(15):
        marcados = pagina.locator(".ms-drop input[type='checkbox']:checked")
        if marcados.count() == 0:
            break
        try:
            marcados.first.click()
            total_desmarcados += 1
        except Exception:
            break
        pagina.wait_for_timeout(300)

    if total_desmarcados > 0:
        print(f"Desmarquei {total_desmarcados} classe(s) que ainda estavam marcadas.")

    ok_marcar = _marcar_ou_desmarcar_opcao_classe(pagina, texto_marcar)

    try:
        pagina.keyboard.press("Escape")
    except Exception:
        pass
    pagina.wait_for_timeout(500)

    return ok_marcar


def preencher_pesquisa_cpf_cnpj(pagina, cnpj, classe_texto):
    if not selecionar_tipo_pesquisa(pagina, "CPF/CNPJ"):
        return False
    pagina.wait_for_timeout(1000)
    if not preencher_cpf_cnpj(pagina, cnpj):
        return False
    if not definir_classe_unica(pagina, classe_texto):
        return False
    return True


def clicar_consultar(pagina):
    try:
        botao = pagina.get_by_role("button", name=re.compile(r"^\s*Consultar\s*$", re.IGNORECASE))
        if botao.count() > 0 and botao.first.is_visible():
            botao.first.click()
            return True
    except Exception:
        pass
    return False


def aguardar_resultados_consulta(pagina, timeout_segundos=150, padrao=None):
    padrao = padrao or PADRAO_NUMERO_PROCESSO
    print(f"Aguardando os resultados carregarem (até {timeout_segundos}s)...")
    for segundo in range(timeout_segundos):
        try:
            processos = pagina.locator("a").filter(has_text=padrao)
            if processos.count() > 0:
                print(f"Resultados carregados após ~{segundo}s.")
                return processos
        except Exception:
            pass
        pagina.wait_for_timeout(1000)
        if tecla_pular_pressionada():
            break
        if (segundo + 1) % 30 == 0:
            print(f"Ainda aguardando resultados... ({segundo + 1}s)")
    return pagina.locator("a").filter(has_text=padrao)


def garantir_resultados_ano_atual(pagina, timeout_geral=150):
    """
    Checa se o PRIMEIRO processo da lista é do ano atual; se não for,
    clica no cabeçalho 'Data de Autuação' até 3 vezes (conferindo a
    cada clique) para trazer os mais recentes para o topo. Aplica a
    TODOS os tribunais — é seguro mesmo onde nunca é necessário (só
    passa direto sem clicar em nada).
    """
    ano_atual = str(datetime.now().year)
    processos = aguardar_resultados_consulta(pagina, timeout_segundos=timeout_geral)

    def primeiro_e_do_ano_atual(processos_loc):
        if processos_loc.count() == 0:
            return False
        try:
            numero = processos_loc.first.inner_text().strip()
        except Exception:
            return False
        return ano_atual in numero

    if primeiro_e_do_ano_atual(processos):
        return processos

    print()
    print(f"O primeiro processo da lista não parece ser de {ano_atual} — tentando ordenar por 'Data de Autuação'...")

    th_data = pagina.get_by_role("columnheader", name=re.compile("Data de Autua", re.IGNORECASE))
    if th_data.count() == 0:
        th_data = pagina.locator("th").filter(has_text=re.compile(r"Data de Autua", re.IGNORECASE))
    if th_data.count() == 0:
        print("ERRO: não encontrei o cabeçalho 'Data de Autuação' para ordenar.")
        return processos

    alvo = th_data.first
    for tentativa in range(3):
        try:
            alvo.click()
        except Exception as erro:
            print("Erro ao clicar no cabeçalho 'Data de Autuação':", erro)
            break
        pagina.wait_for_timeout(2000)
        try:
            pagina.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        processos = aguardar_resultados_consulta(pagina, timeout_segundos=30)
        if primeiro_e_do_ano_atual(processos):
            print(f"Ordenado corretamente após {tentativa + 1} clique(s).")
            return processos

    print(f"Depois de 3 cliques, o primeiro processo ainda não é de {ano_atual} — seguindo mesmo assim.")
    return processos


def pagina_parece_ser_processo(pagina):
    """
    Detecta se a página atual já É a tela de um processo (em vez de
    uma lista de resultados) — acontece no TJAC quando a consulta tem
    resultado único e redireciona direto, sem lista. Checagem segura
    pra qualquer tribunal.
    """
    try:
        texto = pagina.locator("body").inner_text()
    except Exception:
        return False
    return "Partes e Representantes" in texto


def checar_ano_do_processo(pagina, rotulo):
    ano_atual = str(datetime.now().year)
    try:
        texto = pagina.locator("body").inner_text().replace("\xa0", " ")
    except Exception:
        return None, False

    match = PADRAO_NUMERO_PROCESSO.search(texto)
    if not match:
        print(f"Aviso: não identifiquei o número do processo ({rotulo}) para checar o ano.")
        return None, False

    numero_encontrado = match.group(0)
    e_ano_atual = ano_atual in numero_encontrado

    if e_ano_atual:
        print(f"Processo {numero_encontrado} é do ano atual ({ano_atual}) — OK.")
    else:
        print()
        print(f"!!! ATENÇÃO: PROCESSO ANTIGO — {numero_encontrado} (não é de {ano_atual}) !!!")
        print()

    return numero_encontrado, e_ano_atual


def abrir_primeiro_processo(pagina, contexto, processos_override=None):
    if processos_override is not None:
        processos = processos_override
    else:
        processos = pagina.locator("a").filter(has_text=PADRAO_NUMERO_PROCESSO)

    quantidade = processos.count()
    if quantidade == 0:
        return None

    processo_pagina = None
    try:
        with contexto.expect_page(timeout=15000) as info_popup:
            processos.first.click()
        processo_pagina = info_popup.value
    except Exception:
        processo_pagina = pagina

    try:
        processo_pagina.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    processo_pagina.wait_for_timeout(2000)
    return processo_pagina


def fechar_processo_e_voltar(pagina_principal, processo_pagina):
    if processo_pagina is not None and processo_pagina is not pagina_principal:
        try:
            processo_pagina.close()
        except Exception:
            pass
        try:
            pagina_principal.bring_to_front()
        except Exception:
            pass
        return pagina_principal

    try:
        botao_voltar = pagina_principal.get_by_role(
            "button", name=re.compile(r"^\s*Voltar\s*$", re.IGNORECASE)
        )
        if botao_voltar.count() == 0:
            botao_voltar = pagina_principal.get_by_text(re.compile(r"^\s*Voltar\s*$", re.IGNORECASE))
        if botao_voltar.count() > 0:
            botao_voltar.first.click()
            pagina_principal.wait_for_timeout(2000)
    except Exception:
        pass

    return pagina_principal


def recarregar_e_refazer_pesquisa(pagina, cnpj_atual, classe_atual, rotulo_tribunal):
    """
    Se falhar ao preencher o filtro numa combinação que não é a
    primeira, a página pode ter ficado num estado inconsistente —
    recarrega do zero e refaz a pesquisa completa.
    """
    print(f"[{rotulo_tribunal}] Falha ao preencher o filtro — recarregando a página...")
    try:
        pagina.reload(wait_until="domcontentloaded", timeout=60000)
    except Exception as erro:
        print(f"[{rotulo_tribunal}] Erro ao recarregar a página:", erro)
        return False

    pagina.wait_for_timeout(2000)

    if not abrir_consulta_processual(pagina):
        print(f"[{rotulo_tribunal}] Não voltei numa tela válida de Consulta Processual depois do reload.")
        return False

    if not preencher_pesquisa_cpf_cnpj(pagina, cnpj_atual, classe_atual):
        print(f"[{rotulo_tribunal}] Não consegui refazer a pesquisa completa nem depois do reload.")
        return False

    print(f"[{rotulo_tribunal}] Pesquisa refeita com sucesso depois do reload.")
    return True


def obter_texto_coluna_parte(pagina, texto_cabecalho, texto_cabecalho_oposto):
    try:
        celulas_cabecalho = pagina.locator("th, td").filter(
            has_text=re.compile(r"^\s*" + re.escape(texto_cabecalho) + r"\s*$", re.IGNORECASE)
        )
        if celulas_cabecalho.count() > 0:
            celula_cabecalho = celulas_cabecalho.first
            tabela = celula_cabecalho.locator("xpath=ancestor::table[1]")
            if tabela.count() > 0:
                indice = celula_cabecalho.evaluate(
                    "el => Array.prototype.indexOf.call(el.parentElement.children, el)"
                )
                linhas = tabela.locator("tbody tr")
                if linhas.count() == 0:
                    linhas = tabela.locator("tr")
                textos = []
                for i in range(linhas.count()):
                    celulas = linhas.nth(i).locator("th, td")
                    if celulas.count() > indice:
                        try:
                            texto_celula = celulas.nth(indice).inner_text().strip()
                        except Exception:
                            texto_celula = ""
                        if texto_celula:
                            textos.append(texto_celula)
                if textos:
                    return "\n".join(textos)
    except Exception:
        pass

    try:
        cabecalho = pagina.get_by_text(
            re.compile(r"^\s*" + re.escape(texto_cabecalho) + r"\s*$", re.IGNORECASE),
            exact=True,
        )
        if cabecalho.count() == 0:
            return None
        elemento = cabecalho.first
        melhor_texto = None
        for _ in range(6):
            pai = elemento.locator("xpath=..")
            if pai.count() == 0:
                break
            try:
                texto_pai = pai.first.inner_text().strip()
            except Exception:
                break
            linhas_pai = [l.strip().upper() for l in texto_pai.split("\n") if l.strip()]
            if texto_cabecalho_oposto.upper() in linhas_pai:
                break
            if len(texto_pai) > len(texto_cabecalho) + 10:
                melhor_texto = texto_pai
            elemento = pai.first
        return melhor_texto
    except Exception:
        return None


def extrair_nome_e_documento(texto_coluna, texto_cabecalho):
    if not texto_coluna:
        return None, None
    linhas = [l.strip() for l in texto_coluna.split("\n") if l.strip()]
    linhas_uteis = [l for l in linhas if l.upper() != texto_cabecalho.upper()]
    if not linhas_uteis:
        return None, None
    primeira_linha = linhas_uteis[0]
    nome = re.split(r"\(", primeira_linha)[0].strip().rstrip("-").strip()
    if not nome:
        nome = primeira_linha
    match_doc = PADRAO_DOCUMENTO_PARENTESES.search(texto_coluna)
    documento = match_doc.group(1) if match_doc else None
    return nome, documento


def extrair_partes_exequente_executado(pagina):
    def pegar_texto_por_ids(seletores):
        for seletor in seletores:
            try:
                loc = pagina.locator(seletor)
                if loc.count() > 0:
                    texto = loc.first.inner_text().strip()
                    texto = re.sub(r"\s+", " ", texto).strip()
                    if texto:
                        return texto
            except Exception:
                continue
        return None

    autor = pegar_texto_por_ids(
        ["#spnNomeParteAutor0", "[id^='spnNomeParteAutor']", "[data-parte='AUTOR']"]
    )
    documento_autor = pegar_texto_por_ids(["#spnCpfParteAutor0", "[id^='spnCpfParteAutor']"])
    reu = pegar_texto_por_ids(
        ["#spnNomeParteReu0", "[id^='spnNomeParteReu']", "[data-parte='REU']"]
    )
    documento_reu = pegar_texto_por_ids(["#spnCpfParteReu0", "[id^='spnCpfParteReu']"])

    if autor or reu:
        return autor, documento_autor, reu, documento_reu

    texto_exequente = obter_texto_coluna_parte(pagina, "EXEQUENTE", "EXECUTADO")
    texto_executado = obter_texto_coluna_parte(pagina, "EXECUTADO", "EXEQUENTE")
    autor, documento_autor = extrair_nome_e_documento(texto_exequente, "EXEQUENTE")
    reu, documento_reu = extrair_nome_e_documento(texto_executado, "EXECUTADO")
    return autor, documento_autor, reu, documento_reu


def expandir_informacoes_adicionais(pagina):
    try:
        elemento = pagina.get_by_text(re.compile(r"Informa[çc][õo]es Adicionais", re.IGNORECASE))
        if elemento.count() > 0:
            elemento.first.click()
            pagina.wait_for_timeout(1500)
            return True
    except Exception:
        pass
    return False


def extrair_valor_causa(pagina):
    try:
        texto = pagina.locator("body").inner_text().replace("\xa0", " ")
    except Exception:
        return None
    match = re.search(r"Valor\s+da\s+Causa\s*[:\n]?\s*(R\$\s*[\d\.,]+)", texto, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(r"Valor\s+da\s+Causa\s*\n\s*(.+)", texto, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


# ============================================================
# CLIQUE INICIAL DE ACESSO — UM POR "modo_link"
# ============================================================
def _clicar_link_por_img_alt(sessao, alt_padrao):
    pagina = sessao.pagina
    img = pagina.locator(f"img[alt*='{alt_padrao}' i]")
    if img.count() == 0:
        return None, False
    ancestor_link = img.first.locator("xpath=ancestor::a[1]")
    alvo_clique = ancestor_link if ancestor_link.count() > 0 else img
    url_antes_clique = pagina.url
    try:
        with sessao.contexto.expect_page(timeout=8000) as info_pagina_nova:
            alvo_clique.first.click()
        return info_pagina_nova.value, True
    except Exception:
        pagina.wait_for_timeout(2000)
        if pagina.url != url_antes_clique:
            return pagina, True
        return None, False


def _clicar_acesso_sc(sessao, tribunal):
    pagina = sessao.pagina
    url_direto = tribunal["url_direto"]
    texto_link = tribunal["texto_link_regex"]

    seletor_href = f"a[href='{url_direto}']"
    seletor_div_texto = "div.tjsc-text-break"
    try:
        pagina.wait_for_selector(f"{seletor_div_texto}, {seletor_href}", state="visible", timeout=10000)
    except Exception:
        pass

    link_eproc = None
    div_texto = pagina.locator(seletor_div_texto).filter(has_text=texto_link)
    if div_texto.count() == 0:
        div_texto = pagina.locator(seletor_div_texto)
    if div_texto.count() > 0:
        ancestor_link = div_texto.first.locator("xpath=ancestor::a[1]")
        if ancestor_link.count() > 0:
            link_eproc = ancestor_link
    if link_eproc is None or link_eproc.count() == 0:
        link_eproc = pagina.locator(seletor_href)

    if link_eproc.count() == 0:
        return None, False

    url_antes_do_clique = pagina.url
    try:
        link_eproc.first.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    try:
        with sessao.contexto.expect_page(timeout=3000) as info_pagina_nova:
            link_eproc.first.click()
        return info_pagina_nova.value, True
    except Exception:
        try:
            pagina.wait_for_url(lambda url: url != url_antes_do_clique, timeout=10000)
        except Exception:
            pass
        if pagina.url != url_antes_do_clique:
            return pagina, True
        return None, False


def _clicar_acesso_mg(sessao, tribunal):
    pagina = sessao.pagina
    contexto = sessao.contexto
    texto_link = tribunal["texto_link_regex"]

    try:
        pagina.wait_for_selector("div.boxgrup", state="visible", timeout=10000)
    except Exception:
        pass

    alvo_clique = None
    boxgrup = pagina.locator("div.boxgrup").filter(has_text=texto_link)
    if boxgrup.count() == 0:
        titulo = pagina.get_by_text(texto_link)
        if titulo.count() > 0:
            ancestor_box = titulo.first.locator("xpath=ancestor::div[contains(@class,'boxgrup')][1]")
            if ancestor_box.count() > 0:
                boxgrup = ancestor_box
    if boxgrup.count() == 0:
        img_icone = pagina.locator("img[src*='eproc_icones_1a_instancia']")
        if img_icone.count() > 0:
            ancestor_box = img_icone.first.locator("xpath=ancestor::div[contains(@class,'boxgrup')][1]")
            boxgrup = ancestor_box if ancestor_box.count() > 0 else img_icone

    if boxgrup.count() > 0:
        ancestor_link = boxgrup.first.locator("xpath=ancestor::a[1]")
        alvo_clique = ancestor_link if ancestor_link.count() > 0 else boxgrup

    if alvo_clique is None or alvo_clique.count() == 0:
        return None, False

    url_antes_do_clique = pagina.url
    try:
        alvo_clique.first.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    try:
        with contexto.expect_page(timeout=5000) as info_pagina_nova:
            alvo_clique.first.click()
        return info_pagina_nova.value, True
    except Exception:
        try:
            pagina.wait_for_url(lambda url: url != url_antes_do_clique, timeout=10000)
        except Exception:
            pass
        if pagina.url != url_antes_do_clique:
            return pagina, True
        try:
            with contexto.expect_page(timeout=5000) as info_pagina_nova:
                alvo_clique.first.click(force=True)
            return info_pagina_nova.value, True
        except Exception:
            try:
                pagina.wait_for_url(lambda url: url != url_antes_do_clique, timeout=15000)
            except Exception:
                pass
            if pagina.url != url_antes_do_clique:
                return pagina, True
    return None, False


def _clicar_acesso_tjto(sessao, tribunal):
    pagina = sessao.pagina
    contexto = sessao.contexto
    texto_link = tribunal["texto_link_regex"]

    try:
        pagina.wait_for_selector("div.card-body.d-flex", state="visible", timeout=10000)
    except Exception:
        pass

    alvo_clique = None
    card = pagina.locator("div.card-body.d-flex").filter(has_text=texto_link)
    if card.count() == 0:
        titulo = pagina.get_by_text(texto_link)
        if titulo.count() > 0:
            ancestor_card = titulo.first.locator("xpath=ancestor::div[contains(@class,'card-body')][1]")
            if ancestor_card.count() > 0:
                card = ancestor_card
    if card.count() == 0:
        icone = pagina.locator("i.tjicon-user-computer")
        if icone.count() > 0:
            ancestor_card = icone.first.locator("xpath=ancestor::div[contains(@class,'card-body')][1]")
            card = ancestor_card if ancestor_card.count() > 0 else icone

    if card.count() > 0:
        ancestor_link = card.first.locator("xpath=ancestor::a[1]")
        alvo_clique = ancestor_link if ancestor_link.count() > 0 else card

    if alvo_clique is None or alvo_clique.count() == 0:
        return None, False

    url_antes_do_clique = pagina.url
    try:
        alvo_clique.first.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    try:
        with contexto.expect_page(timeout=5000) as info_pagina_nova:
            alvo_clique.first.click()
        pagina_nova = info_pagina_nova.value
        try:
            pagina.close()
        except Exception:
            pass
        return pagina_nova, True
    except Exception:
        try:
            pagina.wait_for_url(lambda url: url != url_antes_do_clique, timeout=10000)
        except Exception:
            pass
        if pagina.url != url_antes_do_clique:
            return pagina, True
        try:
            with contexto.expect_page(timeout=5000) as info_pagina_nova:
                alvo_clique.first.click(force=True)
            pagina_nova = info_pagina_nova.value
            try:
                pagina.close()
            except Exception:
                pass
            return pagina_nova, True
        except Exception:
            try:
                pagina.wait_for_url(lambda url: url != url_antes_do_clique, timeout=15000)
            except Exception:
                pass
            if pagina.url != url_antes_do_clique:
                return pagina, True
    return None, False


def _clicar_acesso_tjac(sessao, tribunal):
    pagina = sessao.pagina
    contexto = sessao.contexto
    prefixo_href = tribunal["prefixo_href_resultado"]
    texto_link = tribunal["texto_link_regex"]

    seletor_resultado = f"a[href^='{prefixo_href}']"
    try:
        pagina.wait_for_selector(seletor_resultado, state="visible", timeout=10000)
    except Exception:
        pass

    alvo_clique = pagina.locator(seletor_resultado)
    if alvo_clique.count() == 0:
        texto_botao = pagina.get_by_text(texto_link)
        if texto_botao.count() > 0:
            ancestor_link = texto_botao.first.locator("xpath=ancestor::a[1]")
            if ancestor_link.count() > 0:
                alvo_clique = ancestor_link

    if alvo_clique.count() == 0:
        return None, False

    url_antes_do_clique = pagina.url
    try:
        alvo_clique.first.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    try:
        with contexto.expect_page(timeout=5000) as info_pagina_nova:
            alvo_clique.first.click()
        return info_pagina_nova.value, True
    except Exception:
        try:
            pagina.wait_for_url(lambda url: url != url_antes_do_clique, timeout=10000)
        except Exception:
            pass
        if pagina.url != url_antes_do_clique:
            return pagina, True
        try:
            with contexto.expect_page(timeout=5000) as info_pagina_nova:
                alvo_clique.first.click(force=True)
            return info_pagina_nova.value, True
        except Exception:
            try:
                pagina.wait_for_url(lambda url: url != url_antes_do_clique, timeout=15000)
            except Exception:
                pass
            if pagina.url != url_antes_do_clique:
                return pagina, True
    return None, False


def localizar_e_clicar_acesso(sessao, tribunal):
    modo = tribunal.get("modo_link")
    if modo == "img_alt":
        return _clicar_link_por_img_alt(sessao, tribunal["img_alt_padrao"])
    if modo == "sc":
        return _clicar_acesso_sc(sessao, tribunal)
    if modo == "mg":
        return _clicar_acesso_mg(sessao, tribunal)
    if modo == "tjto":
        return _clicar_acesso_tjto(sessao, tribunal)
    if modo == "tjac":
        return _clicar_acesso_tjac(sessao, tribunal)
    print(f"[{tribunal['nome']}] Modo de link desconhecido: {modo}")
    return None, False


# ============================================================
# LOGIN COMPLETO (genérico para todos os tribunais)
# ============================================================
def fazer_login_eproc(sessao, tribunal):
    nome = tribunal["nome"]
    print()
    print("==========================================")
    print(f" LOGIN — {nome} (eproc)")
    print("==========================================")

    if not navegar_com_retry(sessao.pagina, tribunal["url_portal"], tentativas=5, timeout=180000):
        print(f"[{nome}] Não foi possível abrir o portal — pulando este ciclo.")
        return False

    sessao.pagina.wait_for_timeout(4000)
    try:
        sessao.pagina.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass

    fechar_banner_cookies(sessao.pagina)

    pagina_login, clicou_automatico = localizar_e_clicar_acesso(sessao, tribunal)

    if not clicou_automatico and tribunal.get("url_direto"):
        print(f"[{nome}] Abrindo diretamente: {tribunal['url_direto']}")
        if navegar_com_retry(sessao.pagina, tribunal["url_direto"], tentativas=3, timeout=60000):
            pagina_login = sessao.pagina
            clicou_automatico = True

    if not clicou_automatico:
        print(f"[{nome}] Não consegui abrir a tela de login de nenhuma forma — pulando este ciclo.")
        diagnosticar_tela(sessao.pagina, f"{nome}_erro_abrir_login")
        return False

    if pagina_login is not None and pagina_login is not sessao.pagina:
        sessao.pagina = pagina_login
        sessao.pagina.on("dialog", tratar_dialogo)

    try:
        sessao.pagina.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    sessao.pagina.wait_for_timeout(1000)
    try:
        sessao.pagina.bring_to_front()
    except Exception:
        pass

    # Sessão já pode estar ativa (cookies salvos de uma execução
    # anterior) — tenta abrir a Consulta Processual direto antes de
    # qualquer login.
    for _ in range(6):
        if abrir_consulta_processual(sessao.pagina):
            print(f"[{nome}] Sessão já ativa — login automático.")
            return True
        sessao.pagina.wait_for_timeout(1500)

    logou_com_certificado = False
    if tribunal.get("usa_certificado", False):
        logou_com_certificado = tentar_login_certificado(sessao.pagina, nome)

    if not logou_com_certificado:
        ok, pagina_atualizada = tentar_login_usuario_senha(
            sessao.pagina,
            sessao.contexto,
            os.getenv(tribunal["usuario_env"]),
            os.getenv(tribunal["senha_env"]),
            nome,
        )
        sessao.pagina = pagina_atualizada
        if not ok:
            print(f"[{nome}] Falha no login por usuário/senha.")
            return False

    totp_secret_bruto = os.getenv(tribunal["totp_secret_env"]) or ""
    totp_secret = totp_secret_bruto.replace(" ", "").strip().upper()
    tratar_totp_se_necessario(sessao.pagina, totp_secret, nome, timeout_segundos=60)

    for segundo in range(tribunal.get("segundos_espera_geral", 240)):
        sessao.pagina.wait_for_timeout(1000)
        if tecla_pular_pressionada():
            break
        if "openid-connect/auth" not in sessao.pagina.url:
            break
        if (segundo + 1) % 30 == 0:
            print(f"[{nome}] Ainda na tela de autenticação... ({segundo + 1}s)")

    print(f"[{nome}] Login concluído (ou tempo esgotado). URL atual:", sessao.pagina.url)

    # Rede de segurança: em alguns tribunais o 2FA só aparece depois
    # de já termos saído da URL de autenticação.
    tratar_totp_se_necessario(sessao.pagina, totp_secret, nome, timeout_segundos=20)

    return True


# ============================================================
# VARREDURA DE CNPJs x CLASSES (genérico para todos)
# ============================================================
def processar_eproc_tribunal(sessao, tribunal):
    nome = tribunal["nome"]

    if not fazer_login_eproc(sessao, tribunal):
        return

    sessao.salvar_sessao()

    consulta_pronta = False
    for _ in range(4):
        if abrir_consulta_processual(sessao.pagina):
            consulta_pronta = True
            break
        sessao.pagina.wait_for_timeout(2000)

    if not consulta_pronta:
        print(f"[{nome}] Não consegui abrir a tela de Consulta Processual.")
        diagnosticar_tela(sessao.pagina, f"{nome}_erro_abrir_consulta")
        return

    pagina = sessao.pagina
    contexto = sessao.contexto
    timeout_resultados = tribunal.get("timeout_resultados", 150)
    ano_atual = str(datetime.now().year)

    for indice_cnpj, cnpj_atual in enumerate(tribunal["cnpjs"]):
        for indice_classe, classe_atual in enumerate(tribunal["classes"]):
            print()
            print(f"--- {nome} / CNPJ {cnpj_atual} / {classe_atual} ---")

            if indice_cnpj == 0 and indice_classe == 0:
                preencheu = preencher_pesquisa_cpf_cnpj(pagina, cnpj_atual, classe_atual)
                if not preencheu:
                    print(f"[{nome}] Não consegui preencher a pesquisa inicial — pulando este tribunal.")
                    diagnosticar_tela(pagina, f"{nome}_erro_preencher_pesquisa")
                    sessao.pagina = pagina
                    return
            else:
                sucesso_filtro = False
                if selecionar_tipo_pesquisa(pagina, "CPF/CNPJ"):
                    pagina.wait_for_timeout(1000)
                    if preencher_cpf_cnpj(pagina, cnpj_atual):
                        if definir_classe_unica(pagina, classe_atual):
                            sucesso_filtro = True
                        else:
                            print(f"[{nome}] Não consegui trocar a Classe Processual.")
                            diagnosticar_tela(pagina, f"{nome}_erro_trocar_classe")

                if not sucesso_filtro:
                    sucesso_filtro = recarregar_e_refazer_pesquisa(pagina, cnpj_atual, classe_atual, nome)

                if not sucesso_filtro:
                    continue

            clicou = clicar_consultar(pagina)
            processos_encontrados = None
            ja_e_processo = False
            if clicou:
                try:
                    pagina.wait_for_load_state("domcontentloaded", timeout=30000)
                except Exception:
                    pass
                pagina.wait_for_timeout(2000)

                if pagina_parece_ser_processo(pagina):
                    print(f"[{nome}] Consulta redirecionou direto para um processo (resultado único).")
                    ja_e_processo = True
                else:
                    processos_encontrados = garantir_resultados_ano_atual(pagina, timeout_geral=timeout_resultados)

            if ja_e_processo:
                numero_processo, e_ano_atual = checar_ano_do_processo(pagina, f"{nome}_{cnpj_atual}_{classe_atual}")
                if not e_ano_atual:
                    print(f"[{nome}] Processo redirecionado não é do ano atual — pulando por segurança.")
                    pagina = fechar_processo_e_voltar(pagina, pagina)
                    sessao.pagina = pagina
                    continue
                if processo_existe_no_firebase(numero_processo):
                    pagina = fechar_processo_e_voltar(pagina, pagina)
                    sessao.pagina = pagina
                    continue
                processo_pagina = pagina
            else:
                if processos_encontrados is None or processos_encontrados.count() == 0:
                    continue
                try:
                    numero_processo = processos_encontrados.first.inner_text().strip()
                except Exception:
                    numero_processo = None
                if not numero_processo or ano_atual not in numero_processo:
                    print(f"[{nome}] Não confirmei que o primeiro processo é de {ano_atual} — pulando por segurança.")
                    continue
                if processo_existe_no_firebase(numero_processo):
                    continue
                processo_pagina = abrir_primeiro_processo(pagina, contexto, processos_override=processos_encontrados)
                if processo_pagina is None:
                    continue

            autor, documento_autor, reu, documento_reu = extrair_partes_exequente_executado(processo_pagina)
            expandir_informacoes_adicionais(processo_pagina)
            valor_causa = extrair_valor_causa(processo_pagina)

            valor_causa_num = valor_causa_para_float(valor_causa)
            if valor_causa_num is not None and valor_causa_num < VALOR_MINIMO_CAUSA:
                print(
                    f"[{nome}] Valor da causa ({valor_causa}) abaixo de "
                    f"{formatar_moeda_br(VALOR_MINIMO_CAUSA)} — não será salvo."
                )
                pagina = fechar_processo_e_voltar(pagina, processo_pagina)
                sessao.pagina = pagina
                continue

            dados = {
                "numero": numero_processo,
                "reu": reu,
                "documento_reu": documento_reu,
                "classe": classe_atual,
                "tribunal": nome,
                "autor": autor,
                "valor_causa": valor_causa,
                "data_distribuicao": None,
            }

            if not dados.get("numero"):
                print(f"[{nome}] ERRO: não identifiquei o número do processo — não será salvo.")
            else:
                salvar_processo_no_firebase(dados, nome)

            pagina = fechar_processo_e_voltar(pagina, processo_pagina)
            sessao.pagina = pagina

    sessao.pagina = pagina
    print()
    print(f"[{nome}] Ciclo de pesquisas concluído.")


# ============================================================
# EXECUÇÃO PRINCIPAL
# ============================================================
with sync_playwright() as p:
    diagnosticar_env_carregado()

    sessao_eproc = SessaoNavegador(p, HEADLESS, ARQUIVO_SESSAO_EPROC)

    print()
    print("==========================================")
    print(" BOT EPROC — TODOS OS TRIBUNAIS")
    print("==========================================")
    print("Tribunais:", ", ".join(t["nome"] for t in EPROC_TRIBUNAIS))
    print(f"Valor mínimo da causa para salvar: {formatar_moeda_br(VALOR_MINIMO_CAUSA)}")

    while True:
        print()
        print("##########################################")
        print(" NOVA VARREDURA — TODOS OS TRIBUNAIS")
        print("##########################################")

        try:
            resultado_ciclo = {}
            for tribunal in EPROC_TRIBUNAIS:
                nome_tribunal = tribunal["nome"]
                print()
                print(f"--- {nome_tribunal} (eproc) ---")
                try:
                    processar_eproc_tribunal(sessao_eproc, tribunal)
                    resultado_ciclo[nome_tribunal] = "ok (sem erro fatal)"
                except Exception as erro:
                    print(f"ERRO ao processar {nome_tribunal} (eproc):", type(erro).__name__, erro)
                    resultado_ciclo[nome_tribunal] = f"ERRO: {type(erro).__name__}: {erro}"

            print()
            print("==========================================")
            print(" RESUMO DO CICLO")
            print("==========================================")
            for nome_tribunal, status in resultado_ciclo.items():
                print(f"  {nome_tribunal}: {status}")

            print()
            print(f"Aguardando {INTERVALO_ENTRE_VARREDURAS}s antes da próxima varredura...")
            time.sleep(INTERVALO_ENTRE_VARREDURAS)

        except KeyboardInterrupt:
            print()
            print("BOT ENCERRADO PELO USUÁRIO")
            break

        except Exception as erro:
            print()
            print("ERRO GERAL NO LOOP PRINCIPAL:", type(erro).__name__, erro)
            time.sleep(INTERVALO_ENTRE_VARREDURAS)

    sessao_eproc.fechar()
