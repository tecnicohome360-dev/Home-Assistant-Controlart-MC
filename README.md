# ControlArt para Home Assistant

Integração personalizada para os módulos cabeados de automação da **ControlArt**:

- **MD-ETH-MCRL2** — módulo de relés (10 saídas, 12 entradas, 5 canais de motor)
- **MD-ETH-MCDM2** — módulo dimmer (9 saídas dimerizáveis, 12 entradas)

A comunicação é local, via TCP, direto com o módulo (porta padrão `4998`).
Não depende de nuvem nem de servidores externos.

## Recursos

- Saídas de relé como **luzes** liga/desliga.
- Saídas do dimmer como **luzes com brilho**, com suporte a *transition*
  (rampa) e respeito aos valores mín./máx. configurados no módulo.
- Canais de motor como **cortinas/persianas/portões** (`cover`), com posição,
  abrir/fechar/parar e opção de **inverter o sentido do motor** por software.
- Teclas dos **teclados CAN Bus** como **entidades de evento**, prontas para
  usar como gatilho de automações e cenas.
- As 12 **entradas físicas** podem ser expostas como sensores binários
  (opcional).
- **Serviços** de calibração de cortina diretamente pelo Home Assistant.
- Conexão TCP persistente com reconexão automática e ressincronização
  periódica do estado.
- Se o módulo sai da rede, as entidades ficam **indisponíveis** na hora e os
  comandos são descartados — nada fica na fila para ser executado quando o
  módulo voltar.

## Instalação

### Via HACS (recomendado)

1. No HACS, abra **Integrações** → menu → **Repositórios personalizados**.
2. Adicione a URL deste repositório, categoria **Integration**.
3. Procure por **ControlArt Wired Modules** e instale.
4. Reinicie o Home Assistant.

### Instalação manual

1. Copie a pasta `custom_components/controlart` para o diretório
   `custom_components` da sua instalação do Home Assistant.
2. Reinicie o Home Assistant.

## Configuração inicial

1. Vá em **Configurações → Dispositivos e Serviços → Adicionar integração**.
2. Procure por **ControlArt**.
3. Informe o **endereço IP** do módulo e a **porta TCP** (padrão `4998`).
4. A integração identifica sozinha se o módulo é de relé ou dimmer, lê o MAC
   e a versão de firmware, e cria o dispositivo.

Adicione **um módulo de cada vez** — cada módulo é uma integração separada.

> **Importante:** logo após adicionar o módulo nenhuma entidade aparece. As
> entidades só são criadas depois que você mapeia as saídas na tela de
> **Opções** (passo seguinte).

## Tela de opções

Em **Dispositivos e Serviços**, abra a integração do módulo e clique em
**Configurar**. O menu de opções tem as seções abaixo.

### Canais (somente módulo de relé)

Cada canal de motor (0 a 4) controla um **par de relés**: o canal `N` usa os
relés `2N` e `2N+1`. Para cada canal, escolha:

- **Luzes** — os dois relés ficam disponíveis como luzes independentes.
- **Cortina** — o par de relés passa a controlar um motor (`cover`).

### Saídas de luz

Selecione quais saídas devem virar entidades de luz e dê um **nome amigável**
a cada uma. Saídas de canais marcados como cortina não aparecem aqui.

### Cortinas (somente módulo de relé)

Para cada canal configurado como cortina, defina:

- **Nome** da cortina.
- **Classe do dispositivo** (cortina, persiana, portão, etc.).
- **Inverter o sentido do motor** — caso abrir e fechar estejam trocados.
  A inversão é feita por software e pode ser ligada/desligada a qualquer
  momento, sem mexer na calibração do módulo.

> As cortinas precisam estar **calibradas** no módulo para que a posição
> (0–100%) funcione corretamente. Veja a seção de calibração abaixo.

### Teclados CAN Bus

Ao abrir esta seção a integração faz uma varredura (*scan*) da rede CAN Bus.
Os teclados encontrados são listados; selecione os que deseja usar e dê nomes
a eles. Cada tecla vira uma entidade de evento separada.

### Geral

- **Expor as 12 entradas físicas** como sensores binários (desligado por
  padrão).
- **Intervalo de atualização** do status, de 10 a 300 segundos
  (padrão: 30 s).

## Usando teclados em cenas e automações

Cada tecla é uma entidade de evento. Os tipos de evento disponíveis são
`click`, `double_click`, `long_click`, `press` e `release`.

Exemplo de automação que liga uma cena ao clicar na tecla 1 de um teclado:

```yaml
automation:
  - alias: "Cena cinema no teclado da sala"
    trigger:
      - platform: state
        entity_id: event.teclado_sala_tecla_1
        attribute: event_type
        to: "click"
    action:
      - service: scene.turn_on
        target:
          entity_id: scene.cinema
```

## Calibração de cortinas

A calibração é feita pelo serviço `controlart.calibrate`, apontando para a
entidade `cover` desejada. Sequência típica:

1. `action: start_up` — inicia a calibração de subida.
2. `action: stop_up` — quando a cortina chegar ao topo.
3. `action: start_down` — inicia a calibração de descida.
4. `action: stop_down` — quando a cortina chegar embaixo.

Use `action: reset` para apagar a calibração atual.

O serviço `controlart.set_motor_mode` permite alternar o modo do motor entre
`normal` (com feedback de posição) e `no_feedback`.

## Observações desta versão (0.1.0)

- O controle é feito por comandos individuais por entidade. O envio em lote
  para várias saídas ao mesmo tempo ainda não está exposto.
- Não há comando mestre de "ligar/desligar tudo".
- As saídas de relé que não são cortina são expostas apenas como luzes
  (sem entidade `switch`).
- O pareamento canal ↔ relés `{2N, 2N+1}` segue o mapeamento padrão da
  ControlArt.
- Configuração de *backlight* e modo das teclas dos teclados não faz parte
  desta versão.

## Suporte

Problemas e sugestões podem ser registrados no rastreador de issues do
repositório.
