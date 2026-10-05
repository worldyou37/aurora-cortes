<div align="center">

# 🐍 AURORA GLORIOSA CORTES

### Uma central local para transformar vídeos autorizados em cortes prontos para publicar.

**Você escolhe o arquivo. A Aurora analisa os assuntos, prepara os cortes e ajuda a publicar nas suas redes.**

Windows · Linux · painel adaptável a celular · IA executada no computador

</div>

---

## O que é

A AURORA GLORIOSA CORTES é um projeto gratuito para identificar os diferentes assuntos de um vídeo, recortar trechos coerentes, gerar legendas, títulos, descrições e hashtags, e organizar publicações para YouTube Shorts, Instagram Reels e TikTok.

O usuário escolhe o arquivo de vídeo no computador ou celular. A Aurora não baixa vídeos de links. A análise e o processamento acontecem no computador que hospeda o programa; no celular, o painel funciona como uma interface remota na mesma rede Wi-Fi.

> Use apenas vídeos que você criou ou tem autorização para editar e publicar. As plataformas podem limitar conteúdo reutilizado, exigir análise do aplicativo ou restringir a visibilidade de publicações.

## Recursos

- Encontra os assuntos ao longo do vídeo e propõe cortes com começo e fim alinhados à fala.
- Gera legendas, títulos, descrições e hashtags relacionadas ao conteúdo; tendências atuais são opcionais e dependem de uma chave da YouTube Data API.
- Oferece layouts verticais prontos para os cortes.
- Organiza uma fila com modo guiado, modo automático e intervalo de publicação configurável em horas.
- Permite escolher YouTube, Instagram e TikTok e consultar o resultado por rede.
- Inclui tutorial e uma página de conexão que explica cada credencial, com verificações que não publicam.
- Permite enviar um teste privado para YouTube.
- Inclui uma página opcional de apoio por Pix. Doar não é necessário para usar o projeto.

## Instalação simples

### Windows

1. Instale Python 3.10 ou superior pelo [site oficial do Python](https://www.python.org/downloads/). Durante a instalação, marque **Add Python to PATH**.
2. Baixe este projeto e extraia a pasta.
3. Dê dois cliques em **`Instalar-Windows.bat`** e conclua a instalação do Ollama quando a janela oficial abrir. O instalador prepara o ambiente Python e baixa o modelo de IA. A primeira instalação exige internet e vários gigabytes livres.
4. Quando terminar, dê dois cliques em **`Iniciar-Windows.bat`**. O painel abrirá no navegador.
5. No painel, siga **Configuração guiada** e depois **Conectar redes**.

### Linux

1. Tenha Python 3.10 ou superior instalado.
2. Abra a pasta do projeto e execute `./instalar-linux.sh`.
3. Depois execute `./iniciar-linux.sh` e abra o endereço mostrado.
4. No painel, siga **Configuração guiada** e depois **Conectar redes**.

O instalador prepara o Ollama e baixa o modelo local necessário. A primeira instalação pode demorar; deixe o computador ligado e conectado à internet.

## Usar pelo celular

O celular e o computador precisam estar conectados à mesma rede Wi-Fi. Inicie a Aurora no computador e escaneie o QR exibido no painel. Você poderá escolher e enviar vídeos pelo celular; a análise continua sendo feita pelo computador. A interface também se adapta a tablets.

A Aurora não tem código de acesso. Qualquer aparelho na mesma rede local que conheça o endereço do painel pode alterar configurações, enviar arquivos e acionar publicações. Use uma rede confiável e não exponha o painel diretamente à internet.

## Conectar as redes

Abra **🔗 Conectar redes** no painel. A página explica onde configurar cada app, quais permissões pedir e como guardar as credenciais. Os botões de teste verificam o acesso sem publicar.

### YouTube Shorts

É necessário criar credenciais OAuth de aplicativo para computador e habilitar YouTube Data API v3 no Google Cloud. Envie o arquivo OAuth na página **Conectar redes**; na primeira autorização, uma janela do navegador pedirá acesso ao canal. Projetos da API não auditados podem ter limitações de visibilidade.

### Instagram Reels

O fluxo preparado pela Aurora usa Instagram Graph API com Facebook Login. Requer conta profissional (Criador ou Empresa) associada a uma Página do Facebook, um app Meta com as permissões necessárias e autorização da conta. A página orienta como obter o ID profissional e o token Meta. A Meta pode exigir revisão do app para determinados usos.

### TikTok

É necessário criar um app no TikTok for Developers, habilitar Content Posting API/Direct Post, solicitar `video.publish` e autorizar a conta. O fluxo de Login Kit exige configuração própria, incluindo URL de retorno HTTPS, e a página da Aurora aceita o token de usuário resultante. Não cole o App Secret no lugar do token. Apps não auditados podem publicar apenas de forma privada.

**As aprovações pertencem às plataformas.** A Aurora não pode aprovar aplicativos, ampliar limites ou remover restrições por conta própria. A disponibilidade e as condições de API podem mudar.

## Controles e arquivos

- **Entrada:** escolha arquivos de vídeo no painel; formatos comuns como MP4, MOV, MKV, WEBM e M4V são aceitos.
- **Saída:** os cortes são guardados em `output/` por padrão. O painel oferece um navegador de pastas para selecionar outro destino.
- **Intervalo:** escolha o tempo entre publicações em horas, incluindo frações, como 2,5 horas.
- **Fila:** acompanhe o andamento, pause novas ações e escolha modo guiado ou automático.
- **Credenciais:** ficam localmente no computador. Não publique nem compartilhe `youtube_client_secret.json`, `token.json` ou `social_secrets.json`.
- **Configuração avançada:** usuários familiarizados com linha de comando podem editar `config.json`; o arquivo inicial está em `config.example.json`.

## Gratuito e apoio

O uso da Aurora é gratuito e não há cobrança para usar a página de doação. As chamadas das APIs e os limites são controlados pelo Google, Meta e TikTok. A geração local requer espaço, memória e processamento do computador. Uma chave opcional da YouTube Data API pode ajudar a consultar sinais de tendências, dentro da quota que o Google atribuir ao projeto.

Se desejar apoiar o desenvolvimento, abra a aba fixa **♡ Apoie a Aurora** no painel. A contribuição é voluntária. A chave Pix exibida ali pode ser copiada com um toque; confira os dados do destinatário no app do banco antes de confirmar.

## Sobre quem fez

Feito por um militante e simpatizante do **Partido Missão**, com a vontade de colocar ferramentas digitais úteis nas mãos de quem cria, organiza e compartilha ideias. Boa sorte a todos — que a Aurora ajude cada pessoa a levar seu trabalho mais longe, com criatividade, responsabilidade e liberdade.

## Aviso

Este projeto é fornecido como está, para uso local. Títulos, descrições, hashtags e cortes gerados por IA podem conter erros: confira os resultados e as permissões do conteúdo antes de publicar. Nenhuma visualização, alcance, renda ou monetização é garantida. Cada plataforma determina seus próprios termos, direitos autorais, regras de API e políticas de monetização.

## Desenvolvimento (opcional)

Para configurar manualmente, instale Python 3.10 ou superior, crie um ambiente virtual e instale `requirements.txt`. Depois copie `config.example.json` para `config.json` e execute `python web.py`. O tutorial no painel contém instruções adicionais. Os instaladores são o caminho recomendado para começar.

---

<div align="center">Feito com 🖤💛🤍 · AURORA GLORIOSA CORTES</div>
