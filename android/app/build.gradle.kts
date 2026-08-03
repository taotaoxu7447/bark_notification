import org.jetbrains.kotlin.gradle.dsl.JvmTarget

val releaseStoreFile = providers.environmentVariable("AGENTWATCH_STORE_FILE").orNull
val releaseStorePassword = providers.environmentVariable("AGENTWATCH_STORE_PASSWORD").orNull
val releaseKeyAlias = providers.environmentVariable("AGENTWATCH_KEY_ALIAS").orNull
val releaseKeyPassword = providers.environmentVariable("AGENTWATCH_KEY_PASSWORD").orNull
val releaseSigningReady = listOf(
    releaseStoreFile,
    releaseStorePassword,
    releaseKeyAlias,
    releaseKeyPassword,
).all { !it.isNullOrBlank() }

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "io.github.taotaoxu7447.agentwatch"
    compileSdk = 36
    buildToolsVersion = "36.0.0"

    defaultConfig {
        applicationId = "io.github.taotaoxu7447.agentwatch"
        minSdk = 26
        targetSdk = 36
        versionCode = 5
        versionName = "0.4.0"

        buildConfigField("String", "SERVER_BASE_URL", "\"https://64.90.8.184:9444\"")
        buildConfigField("String", "API_PREFIX", "\"/agentwatch/api/v1\"")
        buildConfigField("int", "MAX_CATCH_UP_SECONDS", "21600")
    }

    buildFeatures {
        buildConfig = true
    }

    signingConfigs {
        if (releaseSigningReady) {
            create("release") {
                storeFile = rootProject.file(requireNotNull(releaseStoreFile))
                storePassword = releaseStorePassword
                keyAlias = releaseKeyAlias
                keyPassword = releaseKeyPassword
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildTypes {
        debug {
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-debug"
        }
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            signingConfig = signingConfigs.findByName("release")
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    testOptions {
        unitTests.isReturnDefaultValues = true
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

dependencies {
    implementation("com.squareup.okhttp3:okhttp:5.4.0")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}
