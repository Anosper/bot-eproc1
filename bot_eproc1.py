import os
import re
import json
import time
from datetime import datetime, timedelta

import pyotp
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore

import status_bots


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

# Sempre visível (headless=False) — o anti-bot de alguns eproc
# detecta modo headless e trava a sessão. No GitHub Actions isso
# roda por trás do Xvfb (tela virtual, ninguém vê de verdade, mas
# o navegador "acha" que tem uma janela real).
HEADLESS = False

ARQUIVO_SESSAO_EPROC = "sessao_eproc.json"
INTERVALO_ENTRE_VARREDURAS = 30

NOME_DO_GRUPO = "eproc1"


# ============================================================
# CONFIGURAÇÃO SENSÍVEL (tribunais, CNPJs monitorados, classes,
# valor mínimo) — vem de um secret do GitHub Actions, nunca fica
# escrita no código público.
# ============================================================

_CONFIG_BRUTA = os.getenv("BOT_CONFIG_JSON")

if not _CONFIG_BRUTA:
    raise RuntimeError(
        "BOT_CONFIG_JSON não definido — configure esse secret no "
        "GitHub Actions com o conteúdo do config_real.json."
    )

_CONFIG = json.loads(_CONFIG_BRUTA)

VALOR_MINIMO_CAUSA_EPROC = float(_CONFIG["valor_minimo_causa"])
EPROC_TRIBUNAIS = _CONFIG["tribunais"]

# texto_link_regex vem como string no JSON (JSON não tem tipo
# "regex") — recompila para regex de verdade aqui.
for _tribunal_cfg in EPROC_TRIBUNAIS:
    if "texto_link_regex" in _tribunal_cfg:
        _tribunal_cfg["texto_link_regex"] = re.compile(
            _tribunal_cfg["texto_link_regex"], re.IGNORECASE
        )


def diagnosticar_env_carregado():
    def mascarar(valor):
        if not valor:
            return "(vazio ou não definido)"
        if len(valor) <= 4:
            return f"'{valor[0]}...' (tamanho {len(valor)})"
        return f"'{valor[:2]}...{valor[-2:]}' (tamanho {len(valor)})"

    print()
    print("==========================================")
    print(" DIAGNÓSTICO DO .ENV (sem expor os valores)")
    print("==========================================")
    for tribunal in EPROC_TRIBUNAIS:
        usuario = os.getenv(tribunal["usuario_env"])
        senha = os.getenv(tribunal["senha_env"])
        totp = os.getenv(tribunal["totp_secret_env"])
        print(f"--- {tribunal['nome']} ---")
        print(f"  {tribunal['usuario_env']}:", mascarar(usuario))
        print(f"  {tribunal['senha_env']}:", mascarar(senha))
        print(f"  {tribunal['totp_secret_env']}:", mascarar(totp))
    print("==========================================")


print()
print("==========================================")
print(" CONECTANDO AO FIREBASE (projeto botorion2)")
print("==========================================")
try:
    credencial = credentials.Certificate("firebase-service-account2.json")
    firebase_admin.initialize_app(credencial)
    db = firestore.client()
    print("Firebase conectado com sucesso!")
except Exception as erro:
    print("ERRO AO CONECTAR AO FIREBASE:", type(erro).__name__, erro)
    raise


def tratar_dialogo(dialog):
    print()
    print("--- ALERTA DO EPROC ---")
    print(dialog.message)
    print("-----------------------")
    try:
        dialog.accept()
        print("OK clicado automaticamente.")
    except Exception as erro:
        print("Não foi possível clicar no OK:", erro)


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


def valor_causa_para_float_eproc(valor_causa_texto):
    if not valor_causa_texto:
        return None
    numero = re.sub(r"[^\d,.]", "", valor_causa_texto)
    numero = numero.replace(".", "").replace(",", ".")
    try:
        return float(numero)
    except ValueError:
        return None


def formatar_moeda_br_eproc(valor):
    texto = f"{valor:,.2f}"
    texto = texto.replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {texto}"


def salvar_processo_no_firebase(dados, tribunal_origem):
    numero = dados.get("numero")
    if not numero:
        print("ERRO: processo sem número.")
        return False
    dados["tribunal_origem_consulta"] = tribunal_origem
    dados["dataCaptacao"] = datetime.now()
    dados["emProcessos"] = True
    dados["dataExpiracao"] = dados["dataCaptacao"] + timedelta(days=10)
    dados["data_distribuicao"] = dados["dataCaptacao"]

    try:
        db.collection("processos").document(numero).set(dados)
        print()
        print("PROCESSO SALVO NO FIREBASE")
        print("Número:", dados.get("numero"))
        print("Réu:", dados.get("reu"))
        print("Classe:", dados.get("classe"))
        print("Tribunal:", tribunal_origem)
        print("Valor:", dados.get("valor_causa"))
        return True
    except Exception as erro:
        print("ERRO AO SALVAR NO FIREBASE:", type(erro).__name__, erro)
        return False


def diagnosticar_tela_eproc(pagina, rotulo):
    print(f"DIAGNÓSTICO — {rotulo}")
    try:
        os.makedirs("debug_eproc1", exist_ok=True)
        agora = datetime.now().strftime("%Y%m%d_%H%M%S")
        nome_arquivo = f"debug_eproc1/{rotulo}_{agora}.png".replace(" ", "_")
        pagina.screenshot(path=nome_arquivo, full_page=True)
        print("Screenshot salvo em:", nome_arquivo)
    except Exception as erro:
        print("Não foi possível salvar screenshot:", erro)


def gerar_codigo_totp_eproc(totp_secret):
    if not totp_secret:
        raise RuntimeError("Chave TOTP não definida no .env para este tribunal.")
    return pyotp.TOTP(totp_secret).now()


def parece_tela_de_login_eproc(pagina):
    try:
        if "eproc-sso" in pagina.url or "openid-connect" in pagina.url:
            return True
        if pagina.get_by_label(re.compile("usu[áa]rio", re.IGNORECASE)).count() > 0:
            return True
    except Exception:
        pass
    return False


def tentar_login_usuario_senha_eproc(pagina, contexto, usuario, senha):
    if not usuario or not senha:
        print("ERRO: usuário e/ou senha não definidos no .env para este tribunal.")
        return False, pagina

    if contexto is not None and not parece_tela_de_login_eproc(pagina):
        if len(contexto.pages) > 1:
            pagina = contexto.pages[-1]
            pagina.on("dialog", tratar_dialogo)
        else:
            for p in contexto.pages:
                if parece_tela_de_login_eproc(p):
                    pagina = p
                    pagina.on("dialog", tratar_dialogo)
                    break

    campo_usuario = None
    try:
        loc = pagina.get_by_label(re.compile("usu[áa]rio", re.IGNORECASE))
        if loc.count() > 0:
            campo_usuario = loc.first
    except Exception:
        pass
    if campo_usuario is None:
        for seletor in ["#username", "input[name='username']"]:
            loc = pagina.locator(seletor)
            if loc.count() > 0:
                campo_usuario = loc.first
                break
    if campo_usuario is None:
        loc = pagina.locator("input[type='text']:visible")
        if loc.count() > 0:
            campo_usuario = loc.first

    campo_senha = None
    try:
        loc = pagina.get_by_label(re.compile("senha", re.IGNORECASE))
        if loc.count() > 0:
            campo_senha = loc.first
    except Exception:
        pass
    if campo_senha is None:
        for seletor in ["#password", "input[name='password']"]:
            loc = pagina.locator(seletor)
            if loc.count() > 0:
                campo_senha = loc.first
                break
    if campo_senha is None:
        loc = pagina.locator("input[type='password']:visible")
        if loc.count() > 0:
            campo_senha = loc.first

    if campo_usuario is None or campo_senha is None:
        diagnosticar_tela_eproc(pagina, "campos_login_nao_encontrados")
        return False, pagina

    try:
        campo_usuario.fill(usuario)
        campo_senha.fill(senha)
    except Exception as erro:
        diagnosticar_tela_eproc(pagina, "erro_preencher_login")
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
        except Exception:
            return False, pagina

    pagina.wait_for_timeout(2000)
    return True, pagina


def _preencher_totp_campo_unico_eproc(pagina, campo, totp_secret):
    codigo = gerar_codigo_totp_eproc(totp_secret)
    campo.fill(codigo)
    _confirmar_totp_eproc(pagina, campo)
    return True


def _preencher_totp_multiplos_campos_eproc(pagina, campos_digito, total_digitos, totp_secret):
    codigo = gerar_codigo_totp_eproc(totp_secret)
    for i, digito in enumerate(codigo):
        if i >= total_digitos:
            break
        try:
            campos_digito.nth(i).fill(digito)
        except Exception:
            pass
    ultimo_campo = campos_digito.nth(min(len(codigo), total_digitos) - 1)
    _confirmar_totp_eproc(pagina, ultimo_campo)
    return True


def _confirmar_totp_eproc(pagina, campo_referencia):
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


def tratar_totp_se_necessario_eproc(pagina, totp_secret, timeout_segundos=40):
    seletores_campo_unico = [
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
                        return _preencher_totp_campo_unico_eproc(pagina, campo.first, totp_secret)
                except Exception:
                    continue

        try:
            campo_label = pagina.get_by_label(
                re.compile("c[óo]digo|autentica|verifica|totp|otp", re.IGNORECASE)
            )
            if campo_label.count() > 0 and campo_label.first.is_visible(timeout=1000):
                return _preencher_totp_campo_unico_eproc(pagina, campo_label.first, totp_secret)
        except Exception:
            pass

        try:
            campos_digito = pagina.locator("input[maxlength='1']")
            total_digitos = campos_digito.count()
            if total_digitos >= 6 and campos_digito.first.is_visible(timeout=1000):
                return _preencher_totp_multiplos_campos_eproc(pagina, campos_digito, total_digitos, totp_secret)
        except Exception:
            pass

        pagina.wait_for_timeout(1000)

    return False


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


def _clicar_link_por_href_texto(sessao, url_alvo, texto_regex, timeout_aparecer_ms=40000):
    pagina = sessao.pagina
    contexto = sessao.contexto

    seletor_href = f"a[href='{url_alvo}']"
    try:
        pagina.wait_for_selector(seletor_href, state="visible", timeout=timeout_aparecer_ms)
    except Exception:
        pass

    link = pagina.locator(seletor_href)
    if link.count() == 0:
        link = pagina.get_by_role("link", name=texto_regex)
    if link.count() == 0:
        link = pagina.get_by_text(texto_regex)
    if link.count() == 0:
        return None, False

    url_antes_clique = pagina.url
    try:
        with contexto.expect_page(timeout=8000) as info_pagina_nova:
            link.first.click()
        return info_pagina_nova.value, True
    except Exception:
        try:
            pagina.wait_for_url(lambda url: url != url_antes_clique, timeout=60000)
        except Exception:
            pass
        if pagina.url != url_antes_clique:
            return pagina, True
        return None, False


def localizar_e_clicar_acesso_eproc(sessao, tribunal):
    modo = tribunal.get("modo_link")
    if modo == "img_alt":
        return _clicar_link_por_img_alt(sessao, tribunal["img_alt_padrao"])
    if modo == "href_texto":
        return _clicar_link_por_href_texto(
            sessao,
            tribunal["url_acesso"],
            tribunal["texto_link_regex"],
            timeout_aparecer_ms=tribunal.get("timeout_aparecer_link_ms", 40000),
        )
    return None, False


VALORES_TIPO_PESQUISA_EPROC = {
    "Número de Processo, Chave": "NU",
    "Nome da Parte": "NO",
    "CPF/CNPJ": "CP",
    "OAB": "OA",
    "Originário / Relacionado": "NP",
    "Número CDA / Proc. Adm. CDA.": "CD",
    "IPL": "IP",
}


def abrir_consulta_processual_eproc(pagina):
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
        campo_numero = pagina.get_by_label(re.compile("N[ºo]?\\.?\\s*Processo", re.IGNORECASE))
        if campo_numero.count() > 0:
            return True
    except Exception:
        pass

    return False


def selecionar_tipo_pesquisa_eproc(pagina, texto_opcao):
    campo_tipo = pagina.locator("#selTipoPesquisa")
    if campo_tipo.count() == 0:
        campo_tipo = pagina.locator("select[name='tipoPesquisa']")
    if campo_tipo.count() == 0:
        campo_tipo = pagina.locator("select:visible")
    if campo_tipo.count() == 0:
        return False

    valor = VALORES_TIPO_PESQUISA_EPROC.get(texto_opcao)
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


def preencher_cpf_cnpj_eproc(pagina, valor):
    campo = pagina.locator("input[name='strDocParte']:visible")
    if campo.count() == 0:
        campo = pagina.locator("input[name='strDocParte']")
    if campo.count() == 0:
        try:
            loc = pagina.get_by_label(re.compile("N[úu]mero do CPF/?CNPJ", re.IGNORECASE))
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


def _localizar_botao_classe_processual_eproc(pagina):
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


def _marcar_ou_desmarcar_opcao_classe_eproc(pagina, texto_exato, listar_opcoes_se_nao_achar=True):
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
        return False

    try:
        opcao.first.click()
        return True
    except Exception:
        return False


def selecionar_classe_processual_eproc(pagina, texto_exato):
    campo_classe = _localizar_botao_classe_processual_eproc(pagina)
    if campo_classe is None:
        return False

    try:
        campo_classe.click()
    except Exception:
        return False

    pagina.wait_for_timeout(800)
    resultado = _marcar_ou_desmarcar_opcao_classe_eproc(pagina, texto_exato)

    try:
        pagina.keyboard.press("Escape")
    except Exception:
        pass
    pagina.wait_for_timeout(500)
    return resultado


def trocar_classe_processual_eproc(pagina, texto_desmarcar, texto_marcar):
    campo_classe = _localizar_botao_classe_processual_eproc(pagina)
    if campo_classe is None:
        return False

    try:
        campo_classe.click()
    except Exception:
        return False

    pagina.wait_for_timeout(800)

    ok_desmarcar = True
    if texto_desmarcar:
        ok_desmarcar = _marcar_ou_desmarcar_opcao_classe_eproc(pagina, texto_desmarcar)

    ok_marcar = _marcar_ou_desmarcar_opcao_classe_eproc(pagina, texto_marcar)

    try:
        pagina.keyboard.press("Escape")
    except Exception:
        pass
    pagina.wait_for_timeout(500)

    return ok_desmarcar and ok_marcar


def fechar_processo_e_voltar_eproc(pagina_principal, processo_pagina):
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


def preencher_pesquisa_cpf_cnpj_eproc(pagina, cnpj, classe_texto):
    if not selecionar_tipo_pesquisa_eproc(pagina, "CPF/CNPJ"):
        return False
    pagina.wait_for_timeout(1000)
    if not preencher_cpf_cnpj_eproc(pagina, cnpj):
        return False
    if not selecionar_classe_processual_eproc(pagina, classe_texto):
        return False
    return True


def clicar_consultar_eproc(pagina):
    try:
        botao = pagina.get_by_role("button", name=re.compile(r"^\s*Consultar\s*$", re.IGNORECASE))
        if botao.count() > 0 and botao.first.is_visible():
            botao.first.click()
            return True
    except Exception:
        pass
    return False


def aguardar_resultados_consulta_eproc(pagina, timeout_segundos=90):
    padrao_numero = re.compile(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}")
    for segundo in range(timeout_segundos):
        try:
            processos = pagina.locator("a").filter(has_text=padrao_numero)
            if processos.count() > 0:
                return processos
        except Exception:
            pass
        pagina.wait_for_timeout(1000)
        if (segundo + 1) % 15 == 0:
            print(f"Ainda aguardando resultados... ({segundo + 1}s)")
    return pagina.locator("a").filter(has_text=padrao_numero)


def abrir_primeiro_processo_eproc(pagina, contexto):
    padrao_numero = re.compile(r"\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}")
    processos = pagina.locator("a").filter(has_text=padrao_numero)
    if processos.count() == 0:
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


def obter_texto_coluna_parte_eproc(pagina, texto_cabecalho, texto_cabecalho_oposto):
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


def extrair_nome_e_documento_eproc(texto_coluna, texto_cabecalho):
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

    match_doc = PADRAO_DOCUMENTO_PARENTESES_EPROC.search(texto_coluna)
    documento = match_doc.group(1) if match_doc else None

    return nome, documento


def extrair_partes_exequente_executado_eproc(pagina):
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

    texto_exequente = obter_texto_coluna_parte_eproc(pagina, "EXEQUENTE", "EXECUTADO")
    texto_executado = obter_texto_coluna_parte_eproc(pagina, "EXECUTADO", "EXEQUENTE")

    autor, documento_autor = extrair_nome_e_documento_eproc(texto_exequente, "EXEQUENTE")
    reu, documento_reu = extrair_nome_e_documento_eproc(texto_executado, "EXECUTADO")

    return autor, documento_autor, reu, documento_reu


def expandir_informacoes_adicionais_eproc(pagina):
    try:
        elemento = pagina.get_by_text(re.compile(r"Informa[çc][õo]es Adicionais", re.IGNORECASE))
        if elemento.count() > 0:
            elemento.first.click()
            pagina.wait_for_timeout(1500)
            return True
    except Exception:
        pass
    return False


def extrair_valor_causa_eproc(pagina):
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


PADRAO_DOCUMENTO_PARENTESES_EPROC = re.compile(
    r"\((\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{3}\.\d{3}\.\d{3}-\d{2})\)"
)


# ============================================================
# LOGIN (sem certificado — só usuário/senha/2FA)
# ============================================================
def fazer_login_eproc(sessao, tribunal):
    print()
    print(f"LOGIN — {tribunal['nome']} (eproc)")

    if not navegar_com_retry(sessao.pagina, tribunal["url_portal"], tentativas=5, timeout=180000):
        print(f"[{tribunal['nome']}] Não foi possível abrir o portal — pulando este ciclo.")
        return False

    sessao.pagina.wait_for_timeout(tribunal.get("espera_portal_ms", 2000))
    try:
        sessao.pagina.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass

    pagina_login, clicou_automatico = localizar_e_clicar_acesso_eproc(sessao, tribunal)

    if not clicou_automatico:
        print(f"[{tribunal['nome']}] Não consegui abrir a tela de login automaticamente — pulando este ciclo.")
        diagnosticar_tela_eproc(sessao.pagina, f"{tribunal['nome']}_erro_abrir_login")
        return False

    if pagina_login is not None and pagina_login is not sessao.pagina:
        sessao.pagina = pagina_login
        sessao.pagina.on("dialog", tratar_dialogo)

    try:
        sessao.pagina.wait_for_load_state("domcontentloaded", timeout=30000)
    except Exception:
        pass
    sessao.pagina.wait_for_timeout(1000)
    sessao.pagina.bring_to_front()

    for tentativa in range(6):
        if abrir_consulta_processual_eproc(sessao.pagina):
            print(f"[{tribunal['nome']}] Sessão já ativa — login automático.")
            return True
        sessao.pagina.wait_for_timeout(1500)

    # Servidor não tem certificado digital nem humano disponível —
    # vai direto para usuário e senha.
    ok, pagina_atualizada = tentar_login_usuario_senha_eproc(
        sessao.pagina,
        sessao.contexto,
        os.getenv(tribunal["usuario_env"]),
        os.getenv(tribunal["senha_env"]),
    )
    sessao.pagina = pagina_atualizada
    if not ok:
        print(f"[{tribunal['nome']}] Falha no login por usuário/senha.")
        return False

    totp_secret_bruto = os.getenv(tribunal["totp_secret_env"]) or ""
    totp_secret = totp_secret_bruto.replace(" ", "").strip().upper()
    tratar_totp_se_necessario_eproc(
        sessao.pagina, totp_secret, timeout_segundos=tribunal.get("timeout_totp", 40)
    )

    segundos_espera_geral = tribunal.get("segundos_espera_geral", 180)
    for segundo in range(segundos_espera_geral):
        sessao.pagina.wait_for_timeout(1000)
        if "openid-connect/auth" not in sessao.pagina.url:
            break

    print(f"[{tribunal['nome']}] Login concluído (ou tempo esgotado). URL atual:", sessao.pagina.url)
    return True


# ============================================================
# VARREDURA DE CNPJs/CLASSES (só roda se o login já deu certo)
# ============================================================
def varrer_cnpjs_e_classes_eproc(sessao, tribunal):
    if not abrir_consulta_processual_eproc(sessao.pagina):
        print(f"[{tribunal['nome']}] Não consegui abrir a tela de Consulta Processual.")
        diagnosticar_tela_eproc(sessao.pagina, f"{tribunal['nome']}_erro_abrir_consulta")
        raise RuntimeError("Não foi possível abrir a tela de Consulta Processual.")

    pagina = sessao.pagina
    contexto = sessao.contexto
    ultima_classe_marcada = None
    timeout_resultados = tribunal.get("timeout_resultados", 90)

    for indice_cnpj, cnpj_atual in enumerate(tribunal["cnpjs"]):
        for indice_classe, classe_atual in enumerate(tribunal["classes"]):

            if status_bots.esta_pausado_manualmente(tribunal["nome"]):
                print(f"[{tribunal['nome']}] Pausado manualmente — interrompendo varredura.")
                sessao.pagina = pagina
                return

            print()
            print(f"--- {tribunal['nome']} / CNPJ {cnpj_atual} / {classe_atual} ---")

            if indice_cnpj == 0 and indice_classe == 0:
                preencheu = preencher_pesquisa_cpf_cnpj_eproc(pagina, cnpj_atual, classe_atual)
                if not preencheu:
                    print(f"[{tribunal['nome']}] Não consegui preencher a pesquisa inicial — pulando este ciclo.")
                    diagnosticar_tela_eproc(pagina, f"{tribunal['nome']}_erro_preencher_pesquisa")
                    sessao.pagina = pagina
                    return
                ultima_classe_marcada = classe_atual
            else:
                if not selecionar_tipo_pesquisa_eproc(pagina, "CPF/CNPJ"):
                    print(f"[{tribunal['nome']}] Não consegui reselecionar 'Tipo de Pesquisa' — pulando esta combinação.")
                    continue
                pagina.wait_for_timeout(1000)

                if not preencher_cpf_cnpj_eproc(pagina, cnpj_atual):
                    print(f"[{tribunal['nome']}] Não consegui preencher o CPF/CNPJ — pulando esta combinação.")
                    continue

                if not trocar_classe_processual_eproc(pagina, ultima_classe_marcada, classe_atual):
                    print(f"[{tribunal['nome']}] Não consegui trocar a Classe Processual — pulando esta combinação.")
                    diagnosticar_tela_eproc(pagina, f"{tribunal['nome']}_erro_trocar_classe")
                    continue
                ultima_classe_marcada = classe_atual

            if not clicar_consultar_eproc(pagina):
                continue

            try:
                pagina.wait_for_load_state("domcontentloaded", timeout=30000)
            except Exception:
                pass

            processos = aguardar_resultados_consulta_eproc(pagina, timeout_segundos=timeout_resultados)
            quantidade = processos.count()
            print(f"[{tribunal['nome']}] Processos encontrados: {quantidade}")

            if quantidade == 0:
                continue

            try:
                numero_processo = processos.first.inner_text().strip()
            except Exception:
                numero_processo = None

            if processo_existe_no_firebase(numero_processo):
                continue

            processo_pagina = abrir_primeiro_processo_eproc(pagina, contexto)
            if processo_pagina is None:
                continue

            autor, documento_autor, reu, documento_reu = extrair_partes_exequente_executado_eproc(
                processo_pagina
            )
            expandir_informacoes_adicionais_eproc(processo_pagina)
            valor_causa = extrair_valor_causa_eproc(processo_pagina)

            valor_causa_num = valor_causa_para_float_eproc(valor_causa)
            if valor_causa_num is not None and valor_causa_num < VALOR_MINIMO_CAUSA_EPROC:
                print(
                    f"[{tribunal['nome']}] Valor da causa ({valor_causa}) abaixo de "
                    f"{formatar_moeda_br_eproc(VALOR_MINIMO_CAUSA_EPROC)} — não será salvo."
                )
                pagina = fechar_processo_e_voltar_eproc(pagina, processo_pagina)
                sessao.pagina = pagina
                continue

            dados = {
                "numero": numero_processo,
                "reu": reu,
                "documento_reu": documento_reu,
                "classe": classe_atual,
                "tribunal": tribunal["nome"],
                "autor": autor,
                "valor_causa": valor_causa,
                "data_distribuicao": None,
            }

            if not dados.get("numero"):
                print(f"[{tribunal['nome']}] ERRO: não identifiquei o número do processo — não será salvo.")
            else:
                salvar_processo_no_firebase(dados, tribunal["nome"])

            pagina = fechar_processo_e_voltar_eproc(pagina, processo_pagina)
            sessao.pagina = pagina

    sessao.pagina = pagina
    print()
    print(f"[{tribunal['nome']}] Ciclo de pesquisas concluído.")


# ============================================================
# EXECUÇÃO PRINCIPAL
# ============================================================
with sync_playwright() as p:
    diagnosticar_env_carregado()

    sessao_eproc = SessaoNavegador(p, HEADLESS, ARQUIVO_SESSAO_EPROC)

    print()
    print("==========================================")
    print(" BOT EPROC1 (TJRJ + TJRS) INICIADO")
    print("==========================================")
    print("Tribunais:", ", ".join(t["nome"] for t in EPROC_TRIBUNAIS))
    print(f"Valor mínimo da causa para salvar: {formatar_moeda_br_eproc(VALOR_MINIMO_CAUSA_EPROC)}")

    status_bots.iniciar_heartbeat(NOME_DO_GRUPO)

    IGNORAR_HORARIO = os.getenv("IGNORAR_HORARIO", "").lower() == "true"

    while IGNORAR_HORARIO or status_bots.horario_permitido():

        print()
        print("##########################################")
        print(" NOVA VARREDURA — EPROC1")
        print("##########################################")

        try:
            for tribunal in EPROC_TRIBUNAIS:
                nome_tribunal = tribunal["nome"]

                if not IGNORAR_HORARIO and not status_bots.horario_permitido():
                    print(f"Passou do horário permitido — parando a varredura em {nome_tribunal}.")
                    break

                if status_bots.esta_pausado_manualmente(nome_tribunal):
                    print(f"{nome_tribunal} está pausado manualmente — pulando.")
                    status_bots.atualizar_status(nome_tribunal, NOME_DO_GRUPO, "pausado")
                    continue

                print()
                print(f"--- {nome_tribunal} (eproc) ---")

                try:
                    logado = fazer_login_eproc(sessao_eproc, tribunal)
                except Exception as erro:
                    print(f"ERRO inesperado ao logar em {nome_tribunal}:", type(erro).__name__, erro)
                    status_bots.atualizar_status(
                        nome_tribunal, NOME_DO_GRUPO, "erro",
                        detalhe_erro=f"Falha no login: {erro}"
                    )
                    logado = False

                sessao_eproc.salvar_sessao()

                if not logado:
                    print(f"Pulando {nome_tribunal} — falha no login.")
                    status_bots.atualizar_status(
                        nome_tribunal, NOME_DO_GRUPO, "erro",
                        detalhe_erro="Falha no login (usuário/senha ou 2FA)"
                    )
                    continue

                try:
                    varrer_cnpjs_e_classes_eproc(sessao_eproc, tribunal)
                    status_bots.atualizar_status(nome_tribunal, NOME_DO_GRUPO, "rodando")
                except Exception as erro:
                    print(f"ERRO ao processar {nome_tribunal} (eproc):", type(erro).__name__, erro)
                    status_bots.atualizar_status(
                        nome_tribunal, NOME_DO_GRUPO, "erro",
                        detalhe_erro=f"Erro ao captar processos: {erro}"
                    )

            print()
            print("==========================================")
            print("Varredura completa.")
            print(f"Aguardando {INTERVALO_ENTRE_VARREDURAS}s antes da próxima...")
            print("==========================================")
            time.sleep(INTERVALO_ENTRE_VARREDURAS)

        except Exception as erro:
            print()
            print("ERRO GERAL NO LOOP:", type(erro).__name__, erro)
            time.sleep(INTERVALO_ENTRE_VARREDURAS)

    print()
    print("Fora do horário permitido — encerrando a execução.")
    for tribunal in EPROC_TRIBUNAIS:
        status_bots.atualizar_status(tribunal["nome"], NOME_DO_GRUPO, "fora_do_horario")

    sessao_eproc.fechar()
