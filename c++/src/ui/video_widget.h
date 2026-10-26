// 画面控件：只负责"把 QImage 画到控件上并保持比例"，不参与任何解码/定时。
#pragma once

#include <QImage>
#include <QWidget>

class VideoWidget : public QWidget {
    Q_OBJECT
public:
    explicit VideoWidget(QWidget *parent = nullptr);

    void setImage(const QImage &image);
    void clearImage();
    void setOverlayText(const QString &text);

    QSize sizeHint() const override { return QSize(960, 540); }

protected:
    void paintEvent(QPaintEvent *event) override;

private:
    QImage image_;
    QString overlay_;
};
