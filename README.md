# Ajax Security System Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Full-featured** Home Assistant integration for Ajax Security Systems via the official Cloud API with **real-time synchronization**.

[Version française ci-dessous](#version-française)

## ⚠️ Project Status & Community

This integration is **actively developed** but I'm just getting started with Ajax security systems. I currently own and test with:
- ✅ **Hub 2 Plus**
- ✅ **MotionCam** (Motion detector with photo capture)

Since I don't have access to all Ajax devices yet, **I cannot test every device type**. However, the integration is built on Ajax's official API and should theoretically work with all Ajax devices.

**🤝 Community Help Needed**: If you own other Ajax devices and want to help test and improve this integration, your contributions would be greatly appreciated! Together we can make this the best Ajax integration for Home Assistant.

Issues, pull requests, and feedback are welcome!

## ✨ Key Features

### 🔄 Real-Time Synchronization
- **Instant bidirectional sync** - Changes in Ajax app appear immediately in Home Assistant and vice versa
- **gRPC streaming** - Same technology as the official Ajax mobile app
- **Sub-second updates** - State changes reflected in < 1 second

### 🛡️ Complete Security Control
- ✅ **Arm** (Away mode)
- ✅ **Disarm**
- ✅ **Night Mode**
- ✅ **Panic Button** - Trigger emergency alarm from Home Assistant

### 📱 Device Support

**Tested Devices** (personally verified):
- ✅ **Hub 2 Plus**
- ✅ **MotionCam** - Motion detector with photo capture

**Theoretically Supported** (via official API, not personally tested):
- **Other Hubs** - Hub, Hub Plus, Hub 2, Hub 2 (4G)
- **Motion Detectors** - MotionProtect, CombiProtect
- **Door/Window Contacts** - DoorProtect, DoorProtect Plus
- **Fire Safety** - FireProtect, FireProtect Plus, FireProtect 2
- **Flood Detectors** - LeaksProtect
- **Glass Break** - GlassProtect
- **Sirens** - HomeSiren, StreetSiren
- **Keypads** - KeyPad, KeyPad Plus
- **Smart Devices** - Socket, WallSwitch, Relay

**Note**: The integration uses Ajax's official API and is designed to work with all Ajax devices. If you have devices not listed as tested, they should still work - please report your experience!

### 📊 Rich Entity Support
- **Alarm Control Panel** - Full security system control
- **Binary Sensors** - Motion, door/window, smoke, flood, glass break, tamper, power status
- **Sensors** - Battery level, signal strength, temperature, humidity, device counts, notifications
- **Button** - Panic button for emergency situations

### 🌍 Multi-Hub & Multi-Language
- Support for multiple Ajax Hubs in one Home Assistant instance
- Fully localized in **French** and **English**
- All entities properly translated

## 📦 Installation

### Via HACS (Recommended)

1. Open HACS in Home Assistant
2. Go to "Integrations"
3. Click the 3 dots in the top right corner
4. Select "Custom repositories"
5. Add this repository URL: `https://github.com/foXaCe/ajax-hass`
6. Category: "Integration"
7. Click "Add"
8. Search for "Ajax Security System"
9. Click "Download"
10. Restart Home Assistant

### Manual Installation

1. Download the latest release
2. Copy the `custom_components/ajax` folder to your Home Assistant `config/custom_components/` directory
3. Restart Home Assistant

## ⚙️ Configuration

1. Go to **Settings** → **Devices & Services**
2. Click **"+ Add Integration"**
3. Search for **"Ajax Security System"**
4. Enter your Ajax account credentials:
   - **Email**: Your Ajax account email
   - **Password**: Your Ajax account password
5. Click **Submit**

The integration will automatically discover all your Ajax devices and create entities for them.

## 📖 Usage

### Security Control

Use the **Alarm Control Panel** entity to control your security system:

```yaml
# Example automation: Arm when leaving home
automation:
  - alias: "Arm Ajax when leaving"
    trigger:
      - platform: state
        entity_id: person.your_name
        to: "not_home"
    action:
      - service: alarm_control_panel.alarm_arm_away
        target:
          entity_id: alarm_control_panel.ajax_alarm_home
```

### Panic Button

The panic button entity triggers an emergency alarm:

```yaml
# Example: Add panic button to dashboard
type: button
tap_action:
  action: call-service
  service: button.press
  target:
    entity_id: button.ajax_panic_home
name: Emergency
icon: mdi:alarm-light
```

⚠️ **Warning**: The panic button triggers a **real emergency alarm**. Only use it in genuine emergencies or for testing with your monitoring center's knowledge.

### Sensors & Binary Sensors

All Ajax devices appear as appropriate Home Assistant entities:

- **Motion detectors** → `binary_sensor.ajax_motion_*`
- **Door/window contacts** → `binary_sensor.ajax_door_*`
- **Temperature** → `sensor.ajax_temperature_*`
- **Battery level** → `sensor.ajax_battery_*`
- etc.

## 🔧 Advanced Configuration

### Update Interval

The integration uses **real-time streaming** for instant updates (< 1 second), with a backup polling every 30 seconds. The polling serves as a fallback in case the streaming connection is interrupted. You can adjust the polling interval in `const.py` if needed:

```python
UPDATE_INTERVAL = 30  # seconds
```

### Logging

To enable debug logging, add to your `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.ajax: debug
```

## 🐛 Troubleshooting

### Integration not loading
1. Check Home Assistant logs for errors
2. Verify your Ajax credentials are correct
3. Ensure you have an active internet connection

### Real-time updates not working
1. Check that streaming tasks are started (see logs)
2. Verify firewall allows gRPC connections (port 443)
3. Restart the integration

### Devices not appearing
1. Wait for initial sync to complete (up to 30 seconds)
2. Check that devices are visible in the Ajax app
3. Try reloading the integration

## 🔧 Technical Details

### How This Integration Was Built

This integration was developed through reverse engineering of the official Ajax mobile app API:

1. **SSL Pinning Bypass**: Used Frida to bypass certificate pinning in the Ajax Android app
2. **Traffic Capture**: Captured gRPC traffic using mitmproxy to understand the API structure
3. **Protocol Analysis**: Extracted and analyzed Protocol Buffer (.proto) definitions from the APK
4. **Code Generation**: Generated Python gRPC client code from the extracted protobuf schemas
5. **Implementation**: Built a complete Home Assistant integration using the reverse-engineered API

**Key Technologies**:
- **gRPC/Protobuf**: Ajax uses Google's gRPC protocol for all API communication
- **Streaming API**: Real-time updates via server-sent streaming (same as the official app)
- **Authentication**: Session-based authentication with device registration

The integration communicates **directly** with Ajax's official cloud servers (`mobile-gw.prod.ajax.systems:443`) using the same API as the mobile app.

### Privacy & Security

- ✅ Your credentials are only used to authenticate with Ajax servers
- ✅ No data is sent to any third-party servers
- ✅ All communication is encrypted (TLS/SSL)
- ✅ Session tokens are stored locally in Home Assistant's secure storage
- ✅ The integration is fully open source - you can audit the code

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

If you have Ajax devices that aren't tested yet, your help would be invaluable in improving device support.

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## ⚠️ Disclaimer

This integration is **not officially affiliated** with Ajax Systems. It uses the official Ajax Cloud API but is an independent project.

The integration accesses your Ajax account using your credentials. Your credentials are only used to authenticate with Ajax servers and are not stored or transmitted anywhere else.

## 💰 Support the Project

If this integration is useful to you, you can support its development:

🪙 **Bitcoin**: `bc1qhe4ge22x0anuyeg0fmts6rdmz3t735dnqwt3p7`

Your contributions help improve this project and add new features. Thank you! 🙏

---

# Version Française

**Intégration complète** Home Assistant pour les systèmes de sécurité Ajax Systems via l'API Cloud officielle avec **synchronisation en temps réel**.

## ⚠️ Statut du Projet & Communauté

Cette intégration est **activement développée** mais je débute avec les systèmes de sécurité Ajax. Je possède actuellement et teste avec :
- ✅ **Hub 2 Plus**
- ✅ **MotionCam** (Détecteur de mouvement avec prise de photo)

N'ayant pas encore accès à tous les appareils Ajax, **je ne peux pas tester tous les types d'appareils**. Cependant, l'intégration est construite sur l'API officielle Ajax et devrait théoriquement fonctionner avec tous les appareils Ajax.

**🤝 Aide de la Communauté Nécessaire** : Si vous possédez d'autres appareils Ajax et souhaitez aider à tester et améliorer cette intégration, vos contributions seraient grandement appréciées ! Ensemble, nous pouvons faire de cette intégration la meilleure pour Home Assistant.

Les issues, pull requests et retours d'expérience sont les bienvenus !

## ✨ Fonctionnalités Principales

### 🔄 Synchronisation Temps Réel
- **Sync bidirectionnelle instantanée** - Les changements dans l'app Ajax apparaissent immédiatement dans Home Assistant et vice versa
- **Streaming gRPC** - Même technologie que l'application mobile Ajax officielle
- **Mises à jour sub-secondes** - Changements d'état reflétés en < 1 seconde

### 🛡️ Contrôle Complet de la Sécurité
- ✅ **Armement** (mode absent)
- ✅ **Désarmement**
- ✅ **Mode Nuit**
- ✅ **Bouton Panique** - Déclencher une alarme d'urgence depuis Home Assistant

### 📱 Support des Appareils

**Appareils Testés** (vérifiés personnellement) :
- ✅ **Hub 2 Plus**
- ✅ **MotionCam** - Détecteur de mouvement avec prise de photo

**Théoriquement Supportés** (via l'API officielle, non testés personnellement) :
- **Autres Hubs** - Hub, Hub Plus, Hub 2, Hub 2 (4G)
- **Détecteurs de Mouvement** - MotionProtect, CombiProtect
- **Contacts de Porte/Fenêtre** - DoorProtect, DoorProtect Plus
- **Sécurité Incendie** - FireProtect, FireProtect Plus, FireProtect 2
- **Détecteurs d'Inondation** - LeaksProtect
- **Bris de Vitre** - GlassProtect
- **Sirènes** - HomeSiren, StreetSiren
- **Claviers** - KeyPad, KeyPad Plus
- **Appareils Intelligents** - Socket, WallSwitch, Relay

**Note** : L'intégration utilise l'API officielle Ajax et est conçue pour fonctionner avec tous les appareils Ajax. Si vous avez des appareils non listés comme testés, ils devraient quand même fonctionner - merci de partager votre expérience !

### 📊 Entités Riches
- **Panneau de Contrôle d'Alarme** - Contrôle complet du système de sécurité
- **Capteurs Binaires** - Mouvement, porte/fenêtre, fumée, inondation, bris de vitre, sabotage, état alimentation
- **Capteurs** - Niveau batterie, force signal, température, humidité, compteurs d'appareils, notifications
- **Bouton** - Bouton panique pour les situations d'urgence

### 🌍 Multi-Hub & Multilingue
- Support de plusieurs Hubs Ajax dans une instance Home Assistant
- Entièrement localisé en **Français** et **Anglais**
- Toutes les entités correctement traduites

## 📦 Installation

### Via HACS (Recommandé)

1. Ouvrez HACS dans Home Assistant
2. Allez dans "Intégrations"
3. Cliquez sur les 3 points en haut à droite
4. Sélectionnez "Dépôts personnalisés"
5. Ajoutez l'URL de ce dépôt : `https://github.com/foXaCe/ajax-hass`
6. Catégorie : "Integration"
7. Cliquez sur "Ajouter"
8. Recherchez "Ajax Security System"
9. Cliquez sur "Télécharger"
10. Redémarrez Home Assistant

### Installation Manuelle

1. Téléchargez la dernière version
2. Copiez le dossier `custom_components/ajax` dans votre répertoire Home Assistant `config/custom_components/`
3. Redémarrez Home Assistant

## ⚙️ Configuration

1. Allez dans **Paramètres** → **Appareils et Services**
2. Cliquez sur **"+ Ajouter une intégration"**
3. Recherchez **"Ajax Security System"**
4. Entrez vos identifiants de compte Ajax :
   - **Email** : Votre email de compte Ajax
   - **Mot de passe** : Votre mot de passe de compte Ajax
5. Cliquez sur **Soumettre**

L'intégration découvrira automatiquement tous vos appareils Ajax et créera des entités pour eux.

## 📖 Utilisation

### Contrôle de Sécurité

Utilisez l'entité **Panneau de Contrôle d'Alarme** pour contrôler votre système de sécurité :

```yaml
# Exemple d'automation : Armer en quittant la maison
automation:
  - alias: "Armer Ajax en partant"
    trigger:
      - platform: state
        entity_id: person.votre_nom
        to: "not_home"
    action:
      - service: alarm_control_panel.alarm_arm_away
        target:
          entity_id: alarm_control_panel.ajax_alarm_maison
```

### Bouton Panique

L'entité bouton panique déclenche une alarme d'urgence :

```yaml
# Exemple : Ajouter le bouton panique au tableau de bord
type: button
tap_action:
  action: call-service
  service: button.press
  target:
    entity_id: button.ajax_panic_maison
name: Urgence
icon: mdi:alarm-light
```

⚠️ **Attention** : Le bouton panique déclenche une **vraie alarme d'urgence**. Ne l'utilisez qu'en cas d'urgence réelle ou pour des tests avec l'accord de votre centre de télésurveillance.

### Capteurs et Capteurs Binaires

Tous les appareils Ajax apparaissent comme entités Home Assistant appropriées :

- **Détecteurs de mouvement** → `binary_sensor.ajax_mouvement_*`
- **Contacts porte/fenêtre** → `binary_sensor.ajax_porte_*`
- **Température** → `sensor.ajax_temperature_*`
- **Niveau batterie** → `sensor.ajax_batterie_*`
- etc.

## 🔧 Configuration Avancée

### Intervalle de Mise à Jour

L'intégration utilise le **streaming temps réel** pour des mises à jour instantanées (< 1 seconde), avec un polling de secours toutes les 30 secondes. Le polling sert de solution de repli en cas d'interruption de la connexion streaming. Vous pouvez ajuster l'intervalle de polling dans `const.py` si nécessaire :

```python
UPDATE_INTERVAL = 30  # secondes
```

### Journalisation

Pour activer la journalisation de débogage, ajoutez à votre `configuration.yaml` :

```yaml
logger:
  default: info
  logs:
    custom_components.ajax: debug
```

## 🐛 Dépannage

### L'intégration ne se charge pas
1. Vérifiez les journaux Home Assistant pour les erreurs
2. Vérifiez que vos identifiants Ajax sont corrects
3. Assurez-vous d'avoir une connexion internet active

### Les mises à jour temps réel ne fonctionnent pas
1. Vérifiez que les tâches de streaming sont démarrées (voir les journaux)
2. Vérifiez que le pare-feu autorise les connexions gRPC (port 443)
3. Redémarrez l'intégration

### Les appareils n'apparaissent pas
1. Attendez que la synchronisation initiale soit terminée (jusqu'à 30 secondes)
2. Vérifiez que les appareils sont visibles dans l'app Ajax
3. Essayez de recharger l'intégration

## 🔧 Détails Techniques

### Comment Cette Intégration a été Développée

Cette intégration a été développée par rétro-ingénierie de l'API de l'application mobile Ajax officielle :

1. **Contournement SSL Pinning** : Utilisation de Frida pour contourner l'épinglage de certificat dans l'app Android Ajax
2. **Capture du Trafic** : Capture du trafic gRPC avec mitmproxy pour comprendre la structure de l'API
3. **Analyse de Protocole** : Extraction et analyse des définitions Protocol Buffer (.proto) depuis l'APK
4. **Génération de Code** : Génération du code client gRPC Python à partir des schémas protobuf extraits
5. **Implémentation** : Construction d'une intégration Home Assistant complète utilisant l'API rétro-ingénierie

**Technologies Clés** :
- **gRPC/Protobuf** : Ajax utilise le protocole gRPC de Google pour toute communication API
- **API Streaming** : Mises à jour temps réel via streaming serveur (identique à l'app officielle)
- **Authentification** : Authentification par session avec enregistrement d'appareil

L'intégration communique **directement** avec les serveurs cloud officiels Ajax (`mobile-gw.prod.ajax.systems:443`) en utilisant la même API que l'application mobile.

### Confidentialité & Sécurité

- ✅ Vos identifiants sont uniquement utilisés pour s'authentifier avec les serveurs Ajax
- ✅ Aucune donnée n'est envoyée à des serveurs tiers
- ✅ Toutes les communications sont chiffrées (TLS/SSL)
- ✅ Les tokens de session sont stockés localement dans le stockage sécurisé de Home Assistant
- ✅ L'intégration est entièrement open source - vous pouvez auditer le code

## 🤝 Contribution

Les contributions sont les bienvenues ! N'hésitez pas à soumettre une Pull Request.

Si vous possédez des appareils Ajax qui n'ont pas encore été testés, votre aide serait inestimable pour améliorer la compatibilité.

## 📄 Licence

Ce projet est sous licence MIT - voir le fichier [LICENSE](LICENSE) pour plus de détails.

## ⚠️ Avertissement

Cette intégration n'est **pas officiellement affiliée** à Ajax Systems. Elle utilise l'API Cloud officielle Ajax mais est un projet indépendant.

L'intégration accède à votre compte Ajax en utilisant vos identifiants. Vos identifiants sont uniquement utilisés pour s'authentifier auprès des serveurs Ajax et ne sont ni stockés ni transmis ailleurs.

## 💰 Soutenir le Projet

Si cette intégration vous est utile, vous pouvez soutenir son développement :

🪙 **Bitcoin** : `bc1qhe4ge22x0anuyeg0fmts6rdmz3t735dnqwt3p7`

Vos contributions aident à améliorer ce projet et à ajouter de nouvelles fonctionnalités. Merci ! 🙏
