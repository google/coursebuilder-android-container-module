package com.google.johncox.sample;

import android.app.KeyguardManager;
import android.app.KeyguardManager.KeyguardLock;
import android.content.Context;
import android.test.ActivityInstrumentationTestCase2;
import com.robotium.solo.Solo;
import java.io.File;
import java.util.HashSet;
import junit.framework.Assert;

import com.google.johncox.sample.MyActivity;

/**
 * Created by johncox on 10/29/14.
 */
public class ScreenshotTest extends ActivityInstrumentationTestCase2<MyActivity> {

    private KeyguardLock keyguardLock;
    private String screenshotDir = "/sdcard/Robotium-Screenshots";
    private String screenshotName = "result";
    private String screenshotPath = screenshotName + ".jpg";
    private Solo solo;

    public ScreenshotTest() {
        super(MyActivity.class);
    }

    @Override
    public void setUp() throws Exception {
        disableKeyguard();
        solo = new Solo(getInstrumentation(), getActivity());
        deleteScreenshotFiles();
    }

    @Override
    public void tearDown() throws Exception {
        solo.finishOpenedActivities();
    }

    private void deleteScreenshotFiles() {
        File dir = new File(screenshotDir);

        // No need to recurse; screenshots dir is always at most one level deep.
        if (dir.exists()) {
            for (File screenshot : dir.listFiles()) {
                screenshot.delete();
            }
        }
    }

    private void disableKeyguard() {
        // When the device boots, its screen is locked. This makes headless test
        // instrumentation flaky, so we disable the screen lock under test.
        keyguardLock = ((KeyguardManager) getActivity().getSystemService(
            Context.KEYGUARD_SERVICE)).newKeyguardLock(getClass().getName());
        keyguardLock.disableKeyguard();
    }

    private Boolean screenshotExists(String name) {
        File dir = new File(screenshotDir);

        if (dir.exists()) {
            for (File screenshot : dir.listFiles()) {
                if (screenshot.getName().equals(name)) {
                    return true;
                }
            }
        }

        return false;
    }

    public void test_takeScreenshot() {
        // Here, navigate to the activity you care about and take a screenshot. Grab with:
        // $ adb pull /sdcard/Robotium-Screenshots/result.jpg result.jpg
        solo.takeScreenshot(screenshotName);
        Assert.assertTrue(screenshotExists(screenshotPath));
    }
}
