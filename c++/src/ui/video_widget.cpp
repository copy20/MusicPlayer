#include "ui/video_widget.h"

#include <QPainter>

VideoWidget::VideoWidget(QWidget *parent)
    : QWidget(parent)
{
    setMinimumSize(320, 180);
    setAutoFillBackground(false);
    setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Expanding);
    setAttribute(Qt::WA_OpaquePaintEvent, true);
}

void VideoWidget::setImage(const QImage &image)
{
    image_ = image;
    update();
}

void VideoWidget::clearImage()
{
    image_ = QImage();
    update();
}

void VideoWidget::setOverlayText(const QString &text)
{
    if (overlay_ == text)
        return;
    overlay_ = text;
    update();
}

void VideoWidget::paintEvent(QPaintEvent *)
{
    QPainter p(this);
    p.fillRect(rect(), QColor(12, 14, 18));

    if (!image_.isNull()) {
        // letterbox：按比例缩放居中，绝不拉伸变形
        const QSize target = image_.size().scaled(size(), Qt::KeepAspectRatio);
        const QRect box(QPoint((width() - target.width()) / 2,
                               (height() - target.height()) / 2),
                        target);
        p.setRenderHint(QPainter::SmoothPixmapTransform, true);
        p.drawImage(box, image_);
    } else {
        p.setPen(QColor(120, 128, 140));
        QFont f = p.font();
        f.setPointSize(11);
        p.setFont(f);
        p.drawText(rect(), Qt::AlignCenter,
                   QStringLiteral("拖入视频文件，或使用 文件 → 打开合成演示源"));
    }

    if (!overlay_.isEmpty()) {
        const int h = 34;
        QRect bar(0, height() - h, width(), h);
        p.fillRect(bar, QColor(0, 0, 0, 150));
        p.setPen(QColor(220, 226, 236));
        p.drawText(bar.adjusted(14, 0, -14, 0), Qt::AlignVCenter | Qt::AlignLeft, overlay_);
    }
}
